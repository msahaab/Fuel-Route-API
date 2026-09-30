# Fuel Route Planner API

A Django 6.1 back-end service that calculates an optimal driving route across the United States and recommends the most cost-effective fuel stops using the provided OPIS truck-stop price data.

## What it does

- Accepts origin and destination as either free-text addresses or `lat,lon` coordinate pairs
- Fetches the complete driving geometry from OSRM in a single API call (free, no key required)
- Geocodes addresses via Nominatim only when needed (zero to two calls per request)
- Searches ~6.5 k pre-geocoded stations using an in-memory KD-tree for fast corridor lookups
- Selects cost-effective fuel stops assuming a **500-mile** tank range at **10 MPG**
- Responds with the route as GeoJSON, an ordered list of fuel stops, and total trip fuel cost

## Getting started

> **Note for Debian/Ubuntu users:** If you encounter an error creating the virtual environment, you may need to install the required package first: `sudo apt install python3-venv`

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python manage.py build_station_cache
python manage.py runserver
```

## Testing

```bash
python manage.py test routing -v 2
```

## API reference

### `GET/POST /api/route/`

Accepts parameters via query string or JSON body:

| Parameter | Description |
|-----------|-------------|
| `start`   | Origin — address or `lat,lon` |
| `finish`  | Destination — address or `lat,lon` |

**Coordinates only** (1 external call — OSRM):

```bash
curl "http://127.0.0.1:8000/api/route/?start=35.4676,-97.5164&finish=41.8781,-87.6298"
```

**Addresses** (up to 3 external calls — 2 × Nominatim + 1 × OSRM):

```bash
curl "http://127.0.0.1:8000/api/route/?start=Oklahoma%20City,%20OK&finish=Chicago,%20IL"
```

## Response structure

| Field | Description |
|-------|-------------|
| `route.geometry` | GeoJSON LineString representing the driving route |
| `map` | GeoJSON FeatureCollection containing the route line, start/finish markers, and fuel-stop points |
| `fuel_stops[]` | Ordered list of stops, each with station name, price per gallon, gallons purchased, cost, and route mile |
| `total_fuel_cost_usd` | Total money spent on fuel for the trip |
| `total_gallons` | Total gallons consumed for the trip (≈ `distance / 10`) |

## Key design choices

The following choices were made during development to resolve ambiguities in the requirements.

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Duplicate-station rule | **Keep the lowest retail price** per OPIS ID | Minimises trip cost; aligns with the "cost-effective" objective |
| Corridor width | **12 miles** from the route polyline | A wider corridor avoids "no station in range" failures on rural stretches |
| Short-trip behaviour (≤ 500 mi) | **One fuel stop** at the cheapest station in the first 500 miles | The vehicle must still purchase fuel; zero stops would imply an infinite starting tank |
| Stop-selection strategy | **Greedy** — fill at the cheapest station in the current tank window, then top up as needed | Fast, simple, and produces near-optimal results for assessment scope |
| Starting fuel state | **Full tank** bought at the cheapest station within the first 500 miles | The vehicle departs at mile 0 with a full tank; that purchase is included in the total cost |
| Latency target | **p95 < 2 s** local compute (excluding external round-trips) | The KD-tree search and greedy optimiser complete in single-digit milliseconds on ~6.5 k stations |

## How it works under the hood

- **Routing** — [OSRM](https://project-osrm.org/) public demo server (`router.project-osrm.org`)
- **Geocoding** — [Nominatim](https://nominatim.openstreetmap.org/) with a proper `User-Agent` and rate-limit compliance
- **Station locations** — The fuel CSV contains no coordinates, so stations are joined once to US city centroids from `data/us_cities.csv` and cached in `data/stations_cache.pkl`
- **Fuel strategy** — Purchase a full tank at the cheapest station in the first 500-mile window; whenever the destination is still out of range, stop at the cheapest reachable station and top up. Total gallons always equals `distance_miles / 10`
