import threading
import time

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.config import Settings
from app.delivery import DeliveryService, haversine_m
from app.errors import Unavailable
from app.geo import GeoCandidate
from app.menu import load_menu

MENU_PATH = "data/menu.json"
ORIGIN = (31.46124, 74.38764)  # test restaurant pin, not a real business value


@pytest.fixture(scope="session")
def menu():
    return load_menu(MENU_PATH)


@pytest.fixture
def db_url(tmp_path):
    return f"sqlite:///{tmp_path / 'orders.db'}"


@pytest.fixture
def settings(db_url):
    # _env_file=None: tests never read the real .env.hukm, so they can never reach a real API key.
    # branches_path points at a test-only file so tests never read the real data/branches.json.
    return Settings(_env_file=None, database_url=db_url, menu_path=MENU_PATH, restaurant_lat=ORIGIN[0],
                    restaurant_lon=ORIGIN[1], delivery_radius_m=3000,
                    branches_path="tests/data_branches.json")


def place(label, north_m=0.0, east_m=0.0, precision="exact", confidence=0.9):
    """A map result placed north/east of the restaurant by a number of metres."""
    return GeoCandidate(label, ORIGIN[0] + north_m / 111_000, ORIGIN[1] + east_m / 95_000, precision, confidence)


class FakeGeo:
    """Stands in for Geoapify. `places` maps a lowercase fragment of the query to results."""

    def __init__(self):
        self.places: dict[str, list[GeoCandidate]] = {}
        self.queries: list[str] = []
        self.route_calls = 0
        self.route_m: int | None = None  # None: straight line * 1.3
        self.route_error = False
        self.delays: dict[str, float] = {}  # exact lowercase query -> seconds it takes
        self.in_flight = 0
        self.max_in_flight = 0
        self.fail_next = 0  # the next N geocode calls raise a quick outage
        self._lock = threading.Lock()

    def geocode(self, text, near, radius_m, limit=5):
        lowered = text.lower()
        with self._lock:
            self.queries.append(text)
            fail = self.fail_next > 0
            self.fail_next -= 1 if fail else 0
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            time.sleep(self.delays.get(lowered, 0))
            if fail:
                raise Unavailable("geo_unavailable", "the map service is unavailable")
            for key, found in self.places.items():
                if key in lowered:
                    return list(found)
            return []
        finally:
            with self._lock:
                self.in_flight -= 1

    def route_distance_m(self, origin, dest):
        self.route_calls += 1
        if self.route_error:
            raise Unavailable("geo_timeout", "the map service is too slow right now")
        return self.route_m if self.route_m is not None else round(haversine_m(origin, dest) * 1.3)


@pytest.fixture
def geo():
    fake = FakeGeo()
    fake.places = {
        "phase 4": [place("House 12, Street 5, DHA Phase 4, Lahore", north_m=1200, east_m=500)],
        "phase 8": [place("Street 9, DHA Phase 8, Lahore", north_m=9000, precision="street")],
        "masjid": [place("Phase 5 Jamia Masjid, DHA, Lahore", north_m=1500, precision="exact"),
                   place("Phase 5 Madni Masjid, DHA, Lahore", north_m=2500, east_m=2500, precision="exact")],
        "dha lahore": [place("DHA, Lahore", north_m=800, precision="area", confidence=0.4)],
        "somewhere": [place("Lahore, Pakistan", north_m=6000, precision="too_coarse", confidence=0.2)],
    }
    return fake


@pytest.fixture
def client(settings, geo):
    with TestClient(create_app(settings, models=[], geo=geo)) as c:
        yield c


@pytest.fixture
def delivery_for(settings, geo):
    def make(service):
        return DeliveryService(service, geo, ORIGIN[0], ORIGIN[1], 3000, pin_verified=True)
    return make


def item_named(menu, name):
    return next(i for i in menu.items.values() if i.name == name)


# Chicken Supreme, Deep Pan, Regular 9 inch (1330) + Cheese topping (250) = 1580.
PIZZA = {"crust": 269576, "deep_pan": 1400148, "size": 269577, "regular": 1400151,
         "topping": 269579, "cheese_regular": 1400158, "cheese_small": 1400157}


def pizza_selections(*, topping=True):
    sel = {PIZZA["crust"]: [PIZZA["deep_pan"]], PIZZA["size"]: [PIZZA["regular"]]}
    if topping:
        sel[PIZZA["topping"]] = [PIZZA["cheese_regular"]]
    return sel
