from dataclasses import dataclass

import requests
from django.conf import settings


class RoutingError(RuntimeError):
    pass


@dataclass(frozen=True)
class RouteResult:
    distance_meters: float
    duration_seconds: float
    coordinates: list[list[float]]

    @property
    def distance_miles(self) -> float:
        return self.distance_meters / 1609.344

    def to_geojson(self) -> dict:
        return {
            "type": "LineString",
            "coordinates": self.coordinates,
        }


def fetch_route(start_lat: float, start_lon: float, end_lat: float, end_lon: float) -> RouteResult:

    coords = f"{start_lon},{start_lat};{end_lon},{end_lat}"
    url = f"{settings.OSRM_BASE_URL}/route/v1/driving/{coords}"
    try:
        response = requests.get(
            url,
            params={
                "overview": "full",
                "geometries": "geojson",
                "steps": "false",
                "annotations": "false",
            },
            timeout=30,
        )
        response.raise_for_status()
    except requests.Timeout:
        raise RoutingError(
            "Routing service (OSRM) timed out. Please try again later."
        )
    except requests.ConnectionError:
        raise RoutingError(
            "Could not reach the routing service (OSRM). "
            "Check your internet connection and try again."
        )
    except requests.HTTPError as exc:
        raise RoutingError(
            f"Routing service returned an error: {exc.response.status_code}"
        )
    payload = response.json()
    if payload.get("code") != "Ok" or not payload.get("routes"):
        raise RoutingError(payload.get("message") or "OSRM could not find a route")

    route = payload["routes"][0]
    geometry = route["geometry"]
    if geometry.get("type") != "LineString":
        raise RoutingError("Unexpected OSRM geometry type")

    return RouteResult(
        distance_meters=float(route["distance"]),
        duration_seconds=float(route["duration"]),
        coordinates=geometry["coordinates"],
    )
