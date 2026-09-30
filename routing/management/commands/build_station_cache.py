from django.core.management.base import BaseCommand

from routing.services.stations import build_stations_from_csv, get_station_index, save_cache


class Command(BaseCommand):
    help = "Geocode fuel stations via US city centroids and write data/stations_cache.pkl"

    def handle(self, *args, **options):
        stations = build_stations_from_csv()
        path = save_cache(stations)
        index = get_station_index(force_rebuild=True)
        self.stdout.write(self.style.SUCCESS(f"Cached {len(index)} stations -> {path}"))
