"""
Acceptance and unit tests for the Fuel Route Planner API.

Tests are organised into:
  - Unit tests   : mock all external HTTP calls (fast, always run)
  - Integration  : hit real OSRM / Nominatim (slow, opt-in via --run-integration)

Run all fast tests:
    python manage.py test routing

Run integration tests too:
    python manage.py test routing --run-integration
"""

from __future__ import annotations

import json
import time
from unittest.mock import MagicMock, patch

from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings

from .services.geocoding import GeoPoint, geocode, parse_coordinates
from .services.optimizer import FuelPlan, RouteStation, plan_fuel_stops
from .services.osrm import RouteResult, RoutingError, fetch_route
from .services.stations import Station, StationIndex, build_stations_from_csv
from .views import route_plan


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Oklahoma City coords
OKC_LAT, OKC_LON = 35.4676, -97.5164
# Chicago coords
CHI_LAT, CHI_LON = 41.8781, -87.6298

# A minimal valid OSRM response for mocking
def _make_osrm_response(distance_m: float = 1_200_000, duration_s: float = 36_000):
    """Build a fake OSRM JSON response with a simple 2-point LineString."""
    return {
        "code": "Ok",
        "routes": [
            {
                "distance": distance_m,
                "duration": duration_s,
                "geometry": {
                    "type": "LineString",
                    "coordinates": [
                        [-97.5164, 35.4676],
                        [-95.0, 36.5],
                        [-92.5, 37.5],
                        [-90.0, 38.5],
                        [-87.6298, 41.8781],
                    ],
                },
            }
        ],
    }


def _make_nominatim_response(lat: float, lon: float, name: str = "Test Place"):
    return [{"lat": str(lat), "lon": str(lon), "display_name": name}]


def _build_mock_stations_along_route(
    n: int = 10,
    start_mile: float = 50.0,
    spacing: float = 100.0,
    base_price: float = 3.50,
):
    """Create a list of RouteStation objects spaced evenly along a fake route."""
    stations = []
    for i in range(n):
        mile = start_mile + i * spacing
        stations.append(
            RouteStation(
                station=Station(
                    opis_id=f"FAKE-{i:04d}",
                    name=f"Station {i}",
                    address=f"{100 + i} Main St",
                    city="Testville",
                    state="OK",
                    price=base_price + (i % 3) * 0.10,
                    lat=35.5 + i * 0.5,
                    lon=-97.0 + i * 1.0,
                ),
                route_mile=mile,
                detour_miles=1.0,
            )
        )
    return stations


# ---------------------------------------------------------------------------
# T4 — Missing parameter → 400
# ---------------------------------------------------------------------------

class TestT4MissingParams(SimpleTestCase):
    """FR-4: Missing start or finish returns 400 with a helpful message."""

    def setUp(self):
        self.factory = RequestFactory()

    def test_missing_finish_get(self):
        request = self.factory.get("/api/route/", {"start": "35.4676,-97.5164"})
        response = route_plan(request)
        self.assertEqual(response.status_code, 400)
        body = json.loads(response.render().content)
        self.assertIn("error", body)
        self.assertIn("finish", body["error"].lower())

    def test_missing_start_get(self):
        request = self.factory.get("/api/route/", {"finish": "41.8781,-87.6298"})
        response = route_plan(request)
        self.assertEqual(response.status_code, 400)
        body = json.loads(response.render().content)
        self.assertIn("error", body)

    def test_missing_both_get(self):
        request = self.factory.get("/api/route/")
        response = route_plan(request)
        self.assertEqual(response.status_code, 400)

    def test_missing_finish_post_json(self):
        request = self.factory.post(
            "/api/route/",
            data=json.dumps({"start": "35.4676,-97.5164"}),
            content_type="application/json",
        )
        response = route_plan(request)
        self.assertEqual(response.status_code, 400)

    def test_empty_strings(self):
        request = self.factory.get("/api/route/", {"start": "", "finish": ""})
        response = route_plan(request)
        self.assertEqual(response.status_code, 400)


# ---------------------------------------------------------------------------
# T5 — Non-US location → 4xx
# ---------------------------------------------------------------------------

class TestT5NonUSLocation(SimpleTestCase):
    """FR-3: Coordinates outside USA return 400, not 500."""

    def test_paris_coordinates(self):
        """Paris (48.85, 2.35) — longitude outside USA range → rejected."""
        with self.assertRaises(ValueError) as ctx:
            geocode("48.8566,2.3522")
        self.assertIn("USA", str(ctx.exception))

    def test_london_coordinates(self):
        """London (51.5, -0.12) — longitude outside USA range → rejected."""
        with self.assertRaises(ValueError) as ctx:
            geocode("51.5074,-0.1278")
        self.assertIn("USA", str(ctx.exception))

    def test_southern_hemisphere(self):
        """Buenos Aires (-34.6, -58.4) — latitude outside USA range → rejected."""
        with self.assertRaises(ValueError) as ctx:
            geocode("-34.6037,-58.3816")
        self.assertIn("USA", str(ctx.exception))

    def test_via_view_returns_400(self):
        factory = RequestFactory()
        request = factory.get(
            "/api/route/",
            {"start": "48.8566,2.3522", "finish": "41.8781,-87.6298"},
        )
        response = route_plan(request)
        self.assertEqual(response.status_code, 400)
        body = json.loads(response.render().content)
        self.assertIn("USA", body["error"])


# ---------------------------------------------------------------------------
# T6 — Unresolvable address → 4xx
# ---------------------------------------------------------------------------

class TestT6UnresolvableAddress(SimpleTestCase):
    """FR-3: Unresolvable address returns 400, not 500."""

    @patch("routing.services.geocoding.requests.get")
    def test_unresolvable_address_returns_400(self, mock_get):
        """Nominatim returning empty results → ValueError → 400."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = []
        mock_response.raise_for_status.return_value = None
        mock_get.return_value = mock_response

        factory = RequestFactory()
        request = factory.get(
            "/api/route/",
            {"start": "xyznonexistent123", "finish": "41.8781,-87.6298"},
        )
        response = route_plan(request)
        self.assertEqual(response.status_code, 400)
        body = json.loads(response.render().content)
        self.assertIn("error", body)


# ---------------------------------------------------------------------------
# Geocoding unit tests
# ---------------------------------------------------------------------------

class TestGeocodingUnit(SimpleTestCase):
    """Unit tests for coordinate parsing and the geocode function."""

    def test_parse_valid_coordinates(self):
        pt = parse_coordinates("35.4676,-97.5164")
        self.assertIsNotNone(pt)
        self.assertAlmostEqual(pt.lat, 35.4676)
        self.assertAlmostEqual(pt.lon, -97.5164)

    def test_parse_coordinates_with_spaces(self):
        pt = parse_coordinates("  35.4676 , -97.5164  ")
        self.assertIsNotNone(pt)

    def test_parse_invalid_string(self):
        self.assertIsNone(parse_coordinates("Oklahoma City, OK"))

    def test_parse_out_of_range(self):
        self.assertIsNone(parse_coordinates("91.0,0.0"))
        self.assertIsNone(parse_coordinates("0.0,181.0"))

    def test_geocode_coordinates_usa(self):
        pt = geocode("35.4676,-97.5164")
        self.assertAlmostEqual(pt.lat, 35.4676)

    def test_geocode_coordinates_outside_usa(self):
        with self.assertRaises(ValueError):
            geocode("48.8566,2.3522")  # Paris


# ---------------------------------------------------------------------------
# OSRM timeout / connection error tests
# ---------------------------------------------------------------------------

class TestOSRMErrorHandling(SimpleTestCase):
    """NFR-5: Graceful errors if OSRM times out or fails."""

    @patch("routing.services.osrm.requests.get")
    def test_osrm_timeout(self, mock_get):
        import requests as req
        mock_get.side_effect = req.Timeout("Connection timed out")
        with self.assertRaises(RoutingError) as ctx:
            fetch_route(OKC_LAT, OKC_LON, CHI_LAT, CHI_LON)
        self.assertIn("timed out", str(ctx.exception).lower())

    @patch("routing.services.osrm.requests.get")
    def test_osrm_connection_error(self, mock_get):
        import requests as req
        mock_get.side_effect = req.ConnectionError("DNS failure")
        with self.assertRaises(RoutingError) as ctx:
            fetch_route(OKC_LAT, OKC_LON, CHI_LAT, CHI_LON)
        self.assertIn("could not reach", str(ctx.exception).lower())

    @patch("routing.services.osrm.requests.get")
    def test_osrm_http_error(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 503
        mock_resp.raise_for_status.side_effect = __import__(
            "requests"
        ).HTTPError(response=mock_resp)
        mock_get.return_value = mock_resp
        with self.assertRaises(RoutingError) as ctx:
            fetch_route(OKC_LAT, OKC_LON, CHI_LAT, CHI_LON)
        self.assertIn("503", str(ctx.exception))


# ---------------------------------------------------------------------------
# Nominatim timeout / connection error tests
# ---------------------------------------------------------------------------

class TestNominatimErrorHandling(SimpleTestCase):
    """NFR-5: Graceful errors if Nominatim times out or fails."""

    @patch("routing.services.geocoding.requests.get")
    def test_nominatim_timeout(self, mock_get):
        import requests as req
        mock_get.side_effect = req.Timeout("Connection timed out")
        with self.assertRaises(RuntimeError) as ctx:
            geocode("Some City, OK")
        self.assertIn("timed out", str(ctx.exception).lower())

    @patch("routing.services.geocoding.requests.get")
    def test_nominatim_connection_error(self, mock_get):
        import requests as req
        mock_get.side_effect = req.ConnectionError("DNS failure")
        with self.assertRaises(RuntimeError) as ctx:
            geocode("Some Remote Town, TX")
        self.assertIn("unable to connect", str(ctx.exception).lower())


# ---------------------------------------------------------------------------
# Optimizer unit tests
# ---------------------------------------------------------------------------

class TestOptimizerUnit(SimpleTestCase):
    """Unit tests for the fuel stop optimizer logic."""

    def test_short_trip_one_stop(self):
        """FR-11: Trip ≤ 500 mi → exactly 1 fuel stop."""
        stations = _build_mock_stations_along_route(n=3, start_mile=50, spacing=100)
        plan = plan_fuel_stops(stations, total_distance_miles=300, max_range_miles=500, mpg=10)
        self.assertEqual(len(plan.stops), 1)
        self.assertAlmostEqual(plan.total_gallons, 30.0, places=1)

    def test_long_trip_multiple_stops(self):
        """FR-10: Every leg ≤ 500 miles on a long trip."""
        stations = _build_mock_stations_along_route(
            n=15, start_mile=50, spacing=100, base_price=3.00
        )
        plan = plan_fuel_stops(
            stations, total_distance_miles=1400, max_range_miles=500, mpg=10
        )
        self.assertGreater(len(plan.stops), 1)

        # Verify no leg exceeds 500 miles
        prev_mile = 0.0
        for stop in plan.stops:
            leg = stop.route_mile - prev_mile
            self.assertLessEqual(leg, 500.0 + 1e-6, f"Leg to mile {stop.route_mile} is {leg}")
            prev_mile = stop.route_mile

    def test_cost_consistency(self):
        """FR-18: total_gallons ≈ distance / mpg and cost sums match."""
        stations = _build_mock_stations_along_route(n=10, start_mile=50, spacing=120)
        plan = plan_fuel_stops(
            stations, total_distance_miles=1000, max_range_miles=500, mpg=10
        )
        expected_gallons = 1000 / 10
        self.assertAlmostEqual(plan.total_gallons, expected_gallons, places=1)

        stop_cost_sum = sum(s.cost_usd for s in plan.stops)
        self.assertAlmostEqual(plan.total_cost_usd, stop_cost_sum, places=2)

    def test_no_stations_raises(self):
        """No stations in first 500 mi should raise ValueError."""
        stations = _build_mock_stations_along_route(
            n=3, start_mile=600, spacing=100
        )
        with self.assertRaises(ValueError):
            plan_fuel_stops(stations, total_distance_miles=1000, max_range_miles=500, mpg=10)


# ---------------------------------------------------------------------------
# Station index / cache unit tests
# ---------------------------------------------------------------------------

class TestStationIndex(SimpleTestCase):
    """Tests for the StationIndex KD-tree wrapper."""

    def test_query_finds_nearby(self):
        stations = [
            Station("1", "A", "1 St", "Tulsa", "OK", 3.50, 36.1540, -95.9928),
            Station("2", "B", "2 St", "OKC", "OK", 3.60, 35.4676, -97.5164),
            Station("3", "C", "3 St", "Dallas", "TX", 3.40, 32.7767, -96.7970),
        ]
        index = StationIndex(stations)
        results = index.query_ball_points([(36.15, -95.99)], radius_miles=10)
        opis_ids = {s.opis_id for s in results}
        self.assertIn("1", opis_ids)

    def test_empty_stations_raises(self):
        with self.assertRaises(ValueError):
            StationIndex([])


# ---------------------------------------------------------------------------
# T1 — OKC → Chicago (mocked, fast)
# ---------------------------------------------------------------------------

class TestT1OKCToChicagoMocked(SimpleTestCase):
    """T1: Coordinates, Oklahoma City → Chicago (~750 mi), mocked externals."""

    @patch("routing.services.osrm.requests.get")
    def test_full_plan_via_view(self, mock_osrm_get):
        # Mock OSRM to return a ~750 mile route
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = _make_osrm_response(
            distance_m=750 * 1609.344, duration_s=10 * 3600
        )
        mock_osrm_get.return_value = mock_resp

        factory = RequestFactory()
        request = factory.get(
            "/api/route/",
            {"start": f"{OKC_LAT},{OKC_LON}", "finish": f"{CHI_LAT},{CHI_LON}"},
        )
        response = route_plan(request)
        self.assertEqual(response.status_code, 200)

        body = json.loads(response.render().content)
        self.assertIn("route", body)
        self.assertIn("geometry", body["route"])
        self.assertIn("fuel_stops", body)
        self.assertIn("total_fuel_cost_usd", body)
        self.assertIn("total_gallons", body)
        self.assertIn("map", body)
        self.assertEqual(body["map"]["type"], "FeatureCollection")

        # At least 1 fuel stop for a 750 mi trip
        self.assertGreaterEqual(len(body["fuel_stops"]), 1)

        # Cost consistency
        stop_costs = sum(s["cost_usd"] for s in body["fuel_stops"])
        self.assertAlmostEqual(body["total_fuel_cost_usd"], stop_costs, places=1)


# ---------------------------------------------------------------------------
# T7 — Cost check (unit-level)
# ---------------------------------------------------------------------------

class TestT7CostCheck(SimpleTestCase):
    """T7: total_gallons ≈ distance / 10, cost = sum of stop costs."""

    def test_gallons_equal_distance_div_mpg(self):
        stations = _build_mock_stations_along_route(n=12, start_mile=30, spacing=90)
        for distance in [400, 750, 1200]:
            plan = plan_fuel_stops(
                stations,
                total_distance_miles=distance,
                max_range_miles=500,
                mpg=10,
            )
            expected = distance / 10
            self.assertAlmostEqual(
                plan.total_gallons, expected, places=1,
                msg=f"Distance {distance}: expected {expected} gal, got {plan.total_gallons}",
            )
            cost_sum = sum(s.cost_usd for s in plan.stops)
            self.assertAlmostEqual(plan.total_cost_usd, cost_sum, places=2)


# ---------------------------------------------------------------------------
# T8 — Repeat T1 is not slower (KD-tree singleton)
# ---------------------------------------------------------------------------

class TestT8NoCacheRebuild(SimpleTestCase):
    """T8: Second request is not meaningfully slower (no per-request rebuild)."""

    @patch("routing.services.osrm.requests.get")
    def test_second_call_fast(self, mock_osrm_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = _make_osrm_response(
            distance_m=750 * 1609.344, duration_s=10 * 3600
        )
        mock_osrm_get.return_value = mock_resp

        factory = RequestFactory()
        request1 = factory.get(
            "/api/route/",
            {"start": f"{OKC_LAT},{OKC_LON}", "finish": f"{CHI_LAT},{CHI_LON}"},
        )
        # First call (may load cache)
        t0 = time.perf_counter()
        resp1 = route_plan(request1)
        t1 = time.perf_counter()

        # Second call (cache already warm)
        request2 = factory.get(
            "/api/route/",
            {"start": f"{OKC_LAT},{OKC_LON}", "finish": f"{CHI_LAT},{CHI_LON}"},
        )
        t2 = time.perf_counter()
        resp2 = route_plan(request2)
        t3 = time.perf_counter()

        self.assertEqual(resp1.status_code, 200)
        self.assertEqual(resp2.status_code, 200)

        first_time = t1 - t0
        second_time = t3 - t2
        # Second call should be no more than 3x the first
        # (first call may also be fast if cache is already loaded from other tests)
        self.assertLess(
            second_time,
            max(first_time * 3, 2.0),
            f"Second call ({second_time:.3f}s) was much slower than "
            f"first ({first_time:.3f}s); possible per-request cache rebuild.",
        )


# ---------------------------------------------------------------------------
# View-level POST + JSON body test
# ---------------------------------------------------------------------------

class TestPostJsonBody(SimpleTestCase):
    """FR-1: POST with JSON body works correctly."""

    @patch("routing.services.osrm.requests.get")
    def test_post_json_body(self, mock_osrm_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = _make_osrm_response()
        mock_osrm_get.return_value = mock_resp

        factory = RequestFactory()
        request = factory.post(
            "/api/route/",
            data=json.dumps({
                "start": f"{OKC_LAT},{OKC_LON}",
                "finish": f"{CHI_LAT},{CHI_LON}",
            }),
            content_type="application/json",
        )
        response = route_plan(request)
        self.assertEqual(response.status_code, 200)
        body = json.loads(response.render().content)
        self.assertIn("fuel_stops", body)

    def test_post_invalid_json_body(self):
        factory = RequestFactory()
        request = factory.post(
            "/api/route/",
            data="not valid json {{{",
            content_type="application/json",
        )
        response = route_plan(request)
        self.assertEqual(response.status_code, 400)
        body = json.loads(response.render().content)
        self.assertIn("error", body)
