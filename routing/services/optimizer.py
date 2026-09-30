"""Project stations onto a route and choose cost-effective fuel stops."""

from dataclasses import dataclass

import numpy as np
from django.conf import settings
from scipy.spatial import cKDTree

from .stations import MILES_PER_DEG_LAT, MILES_PER_DEG_LON, Station, StationIndex

EARTH_RADIUS_MILES = 3958.8
EPS = 1e-6


def route_metrics(
    coordinates: list[list[float]],
) -> tuple[np.ndarray, np.ndarray, np.ndarray, float]:
    """
    Build per-vertex lat/lon arrays and cumulative distance (miles) along the polyline.

    OSRM GeoJSON coordinates are [lon, lat].
    Returns (lats, lons, cumulative_miles, total_miles).
    """
    if len(coordinates) < 2:
        raise ValueError("Route geometry is too short")

    lons = np.array([c[0] for c in coordinates], dtype=np.float64)
    lats = np.array([c[1] for c in coordinates], dtype=np.float64)

    rlat = np.radians(lats)
    rlon = np.radians(lons)
    dlat = np.diff(rlat)
    dlon = np.diff(rlon)
    a = (
        np.sin(dlat / 2) ** 2
        + np.cos(rlat[:-1]) * np.cos(rlat[1:]) * np.sin(dlon / 2) ** 2
    )
    segment_miles = 2 * EARTH_RADIUS_MILES * np.arcsin(np.sqrt(np.clip(a, 0, 1)))
    cumulative = np.concatenate([[0.0], np.cumsum(segment_miles)])
    return lats, lons, cumulative, float(cumulative[-1])


@dataclass
class RouteStation:
    station: Station
    route_mile: float
    detour_miles: float

    @property
    def price(self) -> float:
        return self.station.price


@dataclass
class FuelStop:
    station: Station
    route_mile: float
    detour_miles: float
    gallons: float
    cost_usd: float
    range_after_fill_miles: float

    def to_dict(self) -> dict:
        return {
            "opis_id": self.station.opis_id,
            "name": self.station.name,
            "address": self.station.address,
            "city": self.station.city,
            "state": self.station.state,
            "price_per_gallon": round(self.station.price, 4),
            "lat": self.station.lat,
            "lon": self.station.lon,
            "route_mile": round(self.route_mile, 2),
            "detour_miles": round(self.detour_miles, 2),
            "gallons": round(self.gallons, 3),
            "cost_usd": round(self.cost_usd, 2),
            "range_after_fill_miles": round(self.range_after_fill_miles, 2),
        }


@dataclass
class FuelPlan:
    stops: list[FuelStop]
    total_cost_usd: float
    total_gallons: float
    total_distance_miles: float
    max_range_miles: float
    mpg: float

    def to_dict(self) -> dict:
        return {
            "fuel_stops": [s.to_dict() for s in self.stops],
            "total_fuel_cost_usd": round(self.total_cost_usd, 2),
            "total_gallons": round(self.total_gallons, 3),
            "vehicle": {
                "max_range_miles": self.max_range_miles,
                "mpg": self.mpg,
                "tank_gallons": self.max_range_miles / self.mpg,
            },
        }


def _sample_route_points(
    lats: np.ndarray, lons: np.ndarray, cumulative: np.ndarray, every_miles: float = 5.0
) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = [(float(lats[0]), float(lons[0]))]
    next_mark = every_miles
    for i in range(1, len(cumulative)):
        if cumulative[i] >= next_mark or i == len(cumulative) - 1:
            points.append((float(lats[i]), float(lons[i])))
            next_mark = cumulative[i] + every_miles
    return points


def stations_along_route(
    index: StationIndex,
    coordinates: list[list[float]],
    corridor_miles: float | None = None,
) -> tuple[list[RouteStation], float]:
    """
    Find fuel stations near the route and project each onto distance-along-route.

    Uses one in-memory spatial pass — no extra map API calls.
    """
    corridor_miles = corridor_miles if corridor_miles is not None else settings.ROUTE_CORRIDOR_MILES
    lats, lons, cumulative, total_miles = route_metrics(coordinates)

    sample_pts = _sample_route_points(lats, lons, cumulative, every_miles=5.0)
    candidates = index.query_ball_points(sample_pts, radius_miles=corridor_miles)

    route_tree = cKDTree(
        np.column_stack([lats * MILES_PER_DEG_LAT, lons * MILES_PER_DEG_LON])
    )
    if not candidates:
        return [], total_miles

    station_coords = np.array(
        [[s.lat * MILES_PER_DEG_LAT, s.lon * MILES_PER_DEG_LON] for s in candidates],
        dtype=np.float64,
    )
    dists, idxs = route_tree.query(station_coords, k=1)

    along: list[RouteStation] = []
    for station, dist_miles, idx in zip(candidates, dists, idxs):
        idx = int(idx)
        if float(dist_miles) > corridor_miles:
            continue
        route_mile = float(cumulative[idx])
        if route_mile < 0 or route_mile > total_miles:
            continue
        along.append(
            RouteStation(
                station=station,
                route_mile=route_mile,
                detour_miles=float(dist_miles),
            )
        )

    along.sort(key=lambda r: (r.route_mile, r.price))
    deduped: list[RouteStation] = []
    bucket_size = 5.0
    for item in along:
        if not deduped:
            deduped.append(item)
            continue
        prev = deduped[-1]
        if item.route_mile - prev.route_mile < bucket_size:
            if item.price < prev.price:
                deduped[-1] = item
        else:
            deduped.append(item)

    return deduped, total_miles


def plan_fuel_stops(
    route_stations: list[RouteStation],
    total_distance_miles: float,
    max_range_miles: float | None = None,
    mpg: float | None = None,
) -> FuelPlan:
    """
    Pick cost-effective fill-ups. We assume we buy a full tank at the start,
    and then stretch fuel to the next cheapest station when we run low.
    """
    max_range_miles = max_range_miles or settings.VEHICLE_MAX_RANGE_MILES
    mpg = mpg or settings.VEHICLE_MPG
    trip_gallons = total_distance_miles / mpg

    stations = sorted(route_stations, key=lambda s: s.route_mile)
    opening = [s for s in stations if s.route_mile <= max_range_miles]
    if not opening:
        raise ValueError(
            "No fuel stations found within the first 500 miles of the route corridor."
        )

    first = min(opening, key=lambda s: (s.price, s.route_mile))
    stops: list[FuelStop] = []

    def append_purchase(station: RouteStation, miles_to_buy: float, range_after: float) -> None:
        if miles_to_buy <= 1e-9:
            return
        gallons = miles_to_buy / mpg
        stops.append(
            FuelStop(
                station=station.station,
                route_mile=station.route_mile,
                detour_miles=station.detour_miles,
                gallons=gallons,
                cost_usd=gallons * station.price,
                range_after_fill_miles=range_after,
            )
        )

    def window_from(from_pos: float, range_left: float) -> list[RouteStation]:
        return [s for s in stations if from_pos < s.route_mile <= from_pos + range_left]

    pos = 0.0
    current_price = first.price
    if total_distance_miles <= max_range_miles:
        append_purchase(first, total_distance_miles, max_range_miles)
        return FuelPlan(
            stops=stops,
            total_cost_usd=sum(s.cost_usd for s in stops),
            total_gallons=sum(s.gallons for s in stops),
            total_distance_miles=total_distance_miles,
            max_range_miles=max_range_miles,
            mpg=mpg,
        )

    append_purchase(first, max_range_miles, max_range_miles)
    fuel_miles = max_range_miles
    last_stop_id = first.station.opis_id

    while pos + fuel_miles < total_distance_miles - 1e-6:
        window = window_from(pos, fuel_miles)
        alt = [s for s in window if s.station.opis_id != last_stop_id]
        if alt:
            window = alt
        if not window:
            raise ValueError(
                "Cannot complete the route with a 500-mile range: "
                "no fuel station within reach along the corridor."
            )

        # No cheaper options within range, so just find the cheapest station
        # in the latter half of the range to push us forward as far as possible
        cheaper = [s for s in window if s.price < current_price]
        if cheaper:
            target = min(cheaper, key=lambda s: s.route_mile)
        else:
            far = [s for s in window if s.route_mile >= pos + fuel_miles * 0.5]
            pool = far or window
            min_price = min(s.price for s in pool)
            tied = [s for s in pool if abs(s.price - min_price) < EPS]
            target = max(tied, key=lambda s: s.route_mile)

        fuel_miles -= target.route_mile - pos
        pos = target.route_mile
        current_price = target.price
        last_stop_id = target.station.opis_id
        remaining = total_distance_miles - pos

        if remaining <= fuel_miles + EPS:
            break

        ahead = window_from(pos, max_range_miles)
        cheaper_ahead = [s for s in ahead if s.price < current_price]
        if remaining <= max_range_miles and not cheaper_ahead:
            miles_to_buy = max(0.0, remaining - fuel_miles)
        elif cheaper_ahead:
            nxt = min(cheaper_ahead, key=lambda s: s.route_mile)
            miles_to_buy = max(0.0, (nxt.route_mile - pos) - fuel_miles)
            miles_to_buy = min(miles_to_buy, max_range_miles - fuel_miles)
        else:
            miles_to_buy = max_range_miles - fuel_miles

        fuel_miles += miles_to_buy
        append_purchase(target, miles_to_buy, fuel_miles)

    total_gallons = sum(s.gallons for s in stops)
    if abs(total_gallons - trip_gallons) > EPS and stops:
        delta = trip_gallons - total_gallons

        if stops[-1].gallons + delta > 0:
            stops[-1].gallons += delta
            stops[-1].cost_usd = stops[-1].gallons * stops[-1].station.price
        else:
            stops[0].gallons += delta
            stops[0].cost_usd = stops[0].gallons * stops[0].station.price
        stops = [s for s in stops if s.gallons > EPS]

    return FuelPlan(
        stops=stops,
        total_cost_usd=sum(s.cost_usd for s in stops),
        total_gallons=sum(s.gallons for s in stops),
        total_distance_miles=total_distance_miles,
        max_range_miles=max_range_miles,
        mpg=mpg,
    )
