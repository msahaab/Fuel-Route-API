from dataclasses import dataclass

from .geocoding import GeoPoint, geocode
from .optimizer import FuelPlan, plan_fuel_stops, stations_along_route
from .osrm import RouteResult, fetch_route
from .stations import get_station_index


@dataclass
class PlanResult:
    start: GeoPoint
    finish: GeoPoint
    route: RouteResult
    fuel: FuelPlan

    def to_map_geojson(self) -> dict:
        """GeoJSON FeatureCollection: route line + start/finish + fuel stops."""
        features: list[dict] = [
            {
                "type": "Feature",
                "properties": {
                    "kind": "route",
                    "distance_miles": round(self.route.distance_miles, 2),
                    "duration_seconds": round(self.route.duration_seconds, 1),
                },
                "geometry": self.route.to_geojson(),
            },
            {
                "type": "Feature",
                "properties": {
                    "kind": "start",
                    "name": self.start.display_name,
                },
                "geometry": {
                    "type": "Point",
                    "coordinates": [self.start.lon, self.start.lat],
                },
            },
            {
                "type": "Feature",
                "properties": {
                    "kind": "finish",
                    "name": self.finish.display_name,
                },
                "geometry": {
                    "type": "Point",
                    "coordinates": [self.finish.lon, self.finish.lat],
                },
            },
        ]
        for i, stop in enumerate(self.fuel.stops, start=1):
            features.append(
                {
                    "type": "Feature",
                    "properties": {
                        "kind": "fuel_stop",
                        "stop_number": i,
                        "name": stop.station.name,
                        "city": stop.station.city,
                        "state": stop.station.state,
                        "price_per_gallon": round(stop.station.price, 4),
                        "gallons": round(stop.gallons, 3),
                        "cost_usd": round(stop.cost_usd, 2),
                        "route_mile": round(stop.route_mile, 2),
                    },
                    "geometry": {
                        "type": "Point",
                        "coordinates": [stop.station.lon, stop.station.lat],
                    },
                }
            )
        return {"type": "FeatureCollection", "features": features}

    def to_dict(self) -> dict:
        fuel_payload = self.fuel.to_dict()
        return {
            "start": self.start.to_dict(),
            "finish": self.finish.to_dict(),
            "route": {
                "distance_miles": round(self.route.distance_miles, 2),
                "duration_seconds": round(self.route.duration_seconds, 1),
                "duration_hours": round(self.route.duration_seconds / 3600.0, 2),
                "geometry": self.route.to_geojson(),
            },
            "map": self.to_map_geojson(),
            "fuel_stops": fuel_payload["fuel_stops"],
            "total_fuel_cost_usd": fuel_payload["total_fuel_cost_usd"],
            "total_gallons": fuel_payload["total_gallons"],
            "vehicle": fuel_payload["vehicle"],
        }


def plan_trip(start: str, finish: str) -> PlanResult:
    """Plans a route, caches nearby stations, and calculates fuel stops."""
    start_point = geocode(start)
    finish_point = geocode(finish)
    route = fetch_route(start_point.lat, start_point.lon, finish_point.lat, finish_point.lon)

    index = get_station_index()
    along, _ = stations_along_route(index, route.coordinates)
    fuel = plan_fuel_stops(along, total_distance_miles=route.distance_miles)

    return PlanResult(
        start=start_point,
        finish=finish_point,
        route=route,
        fuel=fuel,
    )
