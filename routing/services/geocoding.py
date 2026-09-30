import logging
import re
import threading
import time
from dataclasses import dataclass

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

_COORD_RE = re.compile(
    r"^\s*(?P<lat>-?\d+(?:\.\d+)?)\s*,\s*(?P<lon>-?\d+(?:\.\d+)?)\s*$"
)

_cache: dict[str, "GeoPoint"] = {}
_cache_lock = threading.Lock()
_last_request_at = 0.0
_rate_lock = threading.Lock()


@dataclass(frozen=True)
class GeoPoint:
    lat: float
    lon: float
    display_name: str

    def to_dict(self) -> dict:
        return {
            "lat": self.lat,
            "lon": self.lon,
            "display_name": self.display_name,
        }


def _in_continental_usa(lat: float, lon: float) -> bool:
    return 18.0 <= lat <= 72.0 and -180.0 <= lon <= -66.0


def parse_coordinates(value: str) -> GeoPoint | None:
    match = _COORD_RE.match(value)
    if not match:
        return None
    lat = float(match.group("lat"))
    lon = float(match.group("lon"))
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return GeoPoint(lat=lat, lon=lon, display_name=f"{lat},{lon}")


def _throttle() -> None:
    """Respect Nominatim's 1 request/second guideline."""
    global _last_request_at
    with _rate_lock:
        elapsed = time.monotonic() - _last_request_at
        if elapsed < 1.05:
            time.sleep(1.05 - elapsed)
        _last_request_at = time.monotonic()


def geocode(location: str) -> GeoPoint:
    """Resolve a free-text USA location or lat,lon to coordinates."""
    location = (location or "").strip()
    if not location:
        raise ValueError("Location must not be empty")

    coords = parse_coordinates(location)
    if coords is not None:
        if not _in_continental_usa(coords.lat, coords.lon):
            raise ValueError("Coordinates must be within the USA")
        return coords

    cache_key = location.casefold()
    with _cache_lock:
        cached = _cache.get(cache_key)
    if cached is not None:
        return cached

    _throttle()
    try:
        response = requests.get(
            f"{settings.NOMINATIM_BASE_URL}/search",
            params={
                "q": location,
                "format": "json",
                "limit": 1,
                "countrycodes": "us",
                "addressdetails": 0,
            },
            headers={"User-Agent": settings.NOMINATIM_USER_AGENT},
            timeout=20,
        )
        response.raise_for_status()
    except requests.Timeout:
        raise RuntimeError(
            "Nominatim geocoding timed out. Try again in a few seconds."
        )
    except requests.ConnectionError:
        raise RuntimeError(
            "Unable to connect to Nominatim. Is the internet down?"
        )
    except requests.HTTPError as exc:
        raise RuntimeError(
            f"Nominatim returned HTTP {exc.response.status_code}"
        )
    payload = response.json()
    if not payload:
        raise ValueError(f"Could not geocode location: {location!r}")

    hit = payload[0]
    point = GeoPoint(
        lat=float(hit["lat"]),
        lon=float(hit["lon"]),
        display_name=hit.get("display_name") or location,
    )
    if not _in_continental_usa(point.lat, point.lon):
        raise ValueError(f"Location is outside the USA: {location!r}")

    with _cache_lock:
        _cache[cache_key] = point
    logger.info("Geocoded %r -> %.5f,%.5f", location, point.lat, point.lon)
    return point
