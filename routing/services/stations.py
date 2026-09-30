import csv
import logging
import pickle
import threading
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
from django.conf import settings
from scipy.spatial import cKDTree

logger = logging.getLogger(__name__)

MILES_PER_DEG_LAT = 69.0
MILES_PER_DEG_LON = 54.6


@dataclass(frozen=True)
class Station:
    opis_id: str
    name: str
    address: str
    city: str
    state: str
    price: float
    lat: float
    lon: float

    def to_dict(self) -> dict:
        return asdict(self)


class StationIndex:

    def __init__(self, stations: list[Station]):
        if not stations:
            raise ValueError("Station index requires at least one station")
        self.stations = stations
        coords = np.array(
            [
                [s.lat * MILES_PER_DEG_LAT, s.lon * MILES_PER_DEG_LON]
                for s in stations
            ],
            dtype=np.float64,
        )
        self._coords = coords
        self._tree = cKDTree(coords)

    def __len__(self) -> int:
        return len(self.stations)

    def query_ball_points(
        self, points_latlon: list[tuple[float, float]], radius_miles: float
    ) -> list[Station]:
        pts = np.array(
            [[lat * MILES_PER_DEG_LAT, lon * MILES_PER_DEG_LON] for lat, lon in points_latlon],
            dtype=np.float64,
        )
        if pts.size == 0:
            return []
        neighbor_lists = self._tree.query_ball_point(pts, r=radius_miles)
        seen: set[int] = set()
        out: list[Station] = []
        for idxs in neighbor_lists:
            for i in idxs:
                if i not in seen:
                    seen.add(i)
                    out.append(self.stations[i])
        return out


_lock = threading.Lock()
_index: StationIndex | None = None


def _load_city_coordinates(path: Path) -> dict[tuple[str, str], tuple[float, float]]:
    cities: dict[tuple[str, str], tuple[float, float]] = {}
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            key = (row["CITY"].strip().lower(), row["STATE_CODE"].strip().upper())
            cities[key] = (float(row["LATITUDE"]), float(row["LONGITUDE"]))
    return cities


def build_stations_from_csv(
    fuel_csv: Path | None = None,
    cities_csv: Path | None = None,
) -> list[Station]:
    fuel_csv = Path(fuel_csv or settings.FUEL_PRICES_CSV)
    cities_csv = Path(cities_csv or settings.US_CITIES_CSV)
    cities = _load_city_coordinates(cities_csv)

    best_by_id: dict[str, Station] = {}
    total_rows = 0
    canada_excluded = 0
    no_city_match = 0
    bad_price = 0
    duplicates_merged = 0

    with fuel_csv.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            total_rows += 1
            state = row["State"].strip().upper()
            if len(state) != 2 or state in {"AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON", "PE", "QC", "SK", "YT"}:
                canada_excluded += 1
                continue
            city_key = (row["City"].strip().lower(), state)
            coords = cities.get(city_key)
            if coords is None:
                no_city_match += 1
                continue
            try:
                price = float(row["Retail Price"])
            except (TypeError, ValueError):
                bad_price += 1
                continue
            if price <= 0:
                bad_price += 1
                continue

            station = Station(
                opis_id=row["OPIS Truckstop ID"].strip(),
                name=row["Truckstop Name"].strip(),
                address=row["Address"].strip(),
                city=row["City"].strip(),
                state=state,
                price=price,
                lat=coords[0],
                lon=coords[1],
            )
            existing = best_by_id.get(station.opis_id)
            if existing is None:
                best_by_id[station.opis_id] = station
            else:
                duplicates_merged += 1
                if station.price < existing.price:
                    best_by_id[station.opis_id] = station

    stations = list(best_by_id.values())
    logger.info(
        "Station cache built: %d unique stations from %d CSV rows. "
        "Dropped: %d Canadian, %d no city match, %d bad/missing price, "
        "%d duplicate OPIS IDs merged (kept lowest price).",
        len(stations),
        total_rows,
        canada_excluded,
        no_city_match,
        bad_price,
        duplicates_merged,
    )
    return stations


def save_cache(stations: list[Station], path: Path | None = None) -> Path:
    path = Path(path or settings.STATIONS_CACHE_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as fh:
        pickle.dump(stations, fh, protocol=pickle.HIGHEST_PROTOCOL)
    return path


def load_cache(path: Path | None = None) -> list[Station] | None:
    path = Path(path or settings.STATIONS_CACHE_PATH)
    if not path.exists():
        return None
    with path.open("rb") as fh:
        return pickle.load(fh)


def get_station_index(force_rebuild: bool = False) -> StationIndex:
    global _index
    with _lock:
        if _index is not None and not force_rebuild:
            return _index

        stations = None if force_rebuild else load_cache()
        if stations is None:
            stations = build_stations_from_csv()
            save_cache(stations)

        _index = StationIndex(stations)
        return _index
