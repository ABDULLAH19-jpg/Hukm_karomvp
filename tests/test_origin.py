"""The delivery origin is decided in one place, and a disagreement between the two files is loud.

The bug this covers: data/branches.json and the restaurant pin in the environment disagreed, the
branch silently won, and nothing in the log said so. Every delivery decision in the system is
measured from this one point.
"""
import json
import logging

from fastapi.testclient import TestClient

from app.api import create_app
from app.branches import Branch, load_branches
from app.config import Settings
from app.delivery import MISMATCH_TOLERANCE_M, haversine_m, resolve_origin
from tests.conftest import ORIGIN

# A second candidate pin, far from the one in the environment file. The two are the real
# alternatives for this restaurant, and they sit further apart than the whole delivery radius.
OTHER_PIN = (31.5020685, 74.3644417)


def _branch(lat: float | None, lon: float | None, *, from_fallback: bool = False) -> Branch:
    return Branch("cavalry_ground", "Cavalry Ground", "Plot No 68, Cavalry Ground, Lahore",
                  lat, lon, 3000, from_fallback)


# ---- the decision ----------------------------------------------------------

def test_a_branch_with_its_own_coordinates_wins():
    origin = resolve_origin(_branch(*OTHER_PIN), ORIGIN[0], ORIGIN[1], 3000)
    assert (origin.lat, origin.lon) == OTHER_PIN
    assert origin.source == "data/branches.json"
    assert origin.radius_m == 3000


def test_a_far_pin_is_measured_and_reported():
    origin = resolve_origin(_branch(*OTHER_PIN), ORIGIN[0], ORIGIN[1], 3000)
    assert origin.mismatch_m == round(haversine_m(OTHER_PIN, ORIGIN))
    assert origin.mismatch_m > MISMATCH_TOLERANCE_M
    assert origin.mismatch_m > 3000, "the two candidate pins are further apart than the whole radius"


def test_a_pin_that_agrees_is_not_a_mismatch():
    origin = resolve_origin(_branch(*ORIGIN), ORIGIN[0], ORIGIN[1], 3000)
    assert origin.mismatch_m == 0


def test_a_branch_with_no_coordinates_borrows_the_pin(tmp_path):
    path = tmp_path / "branches.json"
    path.write_text(json.dumps([{"id": "b", "name": "B", "address": "Somewhere, Lahore",
                                "radius_m": 2500}]), encoding="utf-8")
    branch = load_branches(str(path), ORIGIN[0], ORIGIN[1])[0]
    assert branch.from_fallback is True
    assert (branch.lat, branch.lon) == ORIGIN

    origin = resolve_origin(branch, ORIGIN[0], ORIGIN[1], 3000)
    assert origin.source == "restaurant pin from the environment"
    assert origin.radius_m == 2500, "the branch keeps its own radius"
    assert origin.mismatch_m is None, "nothing to compare when the pin supplied the point"


def test_no_branch_and_no_pin_means_no_origin():
    assert resolve_origin(None, None, None, 3000) is None


def test_no_branch_falls_back_to_the_pin():
    origin = resolve_origin(None, ORIGIN[0], ORIGIN[1], 2500)
    assert (origin.lat, origin.lon) == ORIGIN
    assert origin.radius_m == 2500
    assert origin.source == "restaurant pin from the environment"


# ---- startup ---------------------------------------------------------------

def test_startup_uses_the_branch_and_logs_the_mismatch(tmp_path, db_url, caplog):
    branches = tmp_path / "branches.json"
    branches.write_text(json.dumps([{
        "id": "cavalry_ground", "name": "Cavalry Ground",
        "address": "Plot No 68, Commercial Area, Cavalry Ground, Lahore",
        "lat": OTHER_PIN[0], "lon": OTHER_PIN[1], "radius_m": 3000}]), encoding="utf-8")
    settings = Settings(_env_file=None, database_url=db_url, menu_path="data/menu.json",
                       restaurant_lat=ORIGIN[0], restaurant_lon=ORIGIN[1], delivery_radius_m=3000,
                       branches_path=str(branches))

    with caplog.at_level(logging.INFO):
        with TestClient(create_app(settings, models=[], geo=None)) as client:
            client.get("/health")
            delivery = client.app.state.delivery

    assert delivery.origin == OTHER_PIN, "the branch file is authoritative for its own point"

    events = [r for r in caplog.records if r.getMessage() in ("delivery_origin", "delivery_origin_mismatch")]
    assert [e.getMessage() for e in events] == ["delivery_origin", "delivery_origin_mismatch"]

    used, mismatch = events
    assert used.source.endswith("branches.json")
    assert (used.lat, used.lon) == OTHER_PIN
    assert mismatch.in_use == str(branches)
    assert (mismatch.config_lat, mismatch.config_lon) == ORIGIN
    assert mismatch.distance_m == round(haversine_m(OTHER_PIN, ORIGIN))
    assert mismatch.tolerance_m == MISMATCH_TOLERANCE_M


def test_startup_is_quiet_when_the_two_files_agree(db_url, caplog):
    """tests/data_branches.json carries the same point as the pin, so nothing should be reported."""
    settings = Settings(_env_file=None, database_url=db_url, menu_path="data/menu.json",
                        restaurant_lat=ORIGIN[0], restaurant_lon=ORIGIN[1], delivery_radius_m=3000,
                        branches_path="tests/data_branches.json")
    with caplog.at_level(logging.INFO):
        with TestClient(create_app(settings, models=[], geo=None)) as client:
            client.get("/health")
    messages = [r.getMessage() for r in caplog.records]
    assert "delivery_origin" in messages
    assert "delivery_origin_mismatch" not in messages
