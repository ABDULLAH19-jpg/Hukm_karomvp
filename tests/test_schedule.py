"""Service-hours and last-order-cutoff tests: Schedule.status(), check_open tool, and enforcement."""
from datetime import datetime, time, timezone

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.config import Settings
from app.db import make_session_factory
from app.errors import DomainError
from app.orders import OrderService
from app.schedule import Schedule, load_schedule
from app.states import OrderState as S
from tests.conftest import ORIGIN, item_named, pizza_selections


def service_for(db_url, menu, schedule):
    return OrderService(make_session_factory(db_url), menu, schedule=schedule)


def freeze_utc(monkeypatch, hour, minute=0):
    """Pin app.schedule.datetime.now to a fixed UTC instant on 2026-10-06."""
    fixed = datetime(2026, 10, 6, hour, minute, 0, tzinfo=timezone.utc)

    class FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed

    monkeypatch.setattr("app.schedule.datetime", FakeDateTime)
    return fixed


# ---- Schedule.status() unit tests -----------------------------------

def test_before_open():
    sched = Schedule(time(11, 0), time(22, 0), time(23, 0))
    now = datetime(2026, 10, 6, 5, 0, 0, tzinfo=timezone.utc)  # 10:00 KHI
    status, message = sched.status(now)
    assert status == "before_open"
    assert "11:00" in message


def test_during_service():
    sched = Schedule(time(11, 0), time(22, 0), time(23, 0))
    now = datetime(2026, 10, 6, 9, 0, 0, tzinfo=timezone.utc)  # 14:00 KHI
    assert sched.status(now)[0] == "open"


def test_after_cutoff():
    sched = Schedule(time(11, 0), time(22, 0), time(23, 0))
    now = datetime(2026, 10, 6, 17, 30, 0, tzinfo=timezone.utc)  # 22:30 KHI
    status, message = sched.status(now)
    assert status == "after_cutoff"
    assert "23:00" in message


def test_after_close():
    sched = Schedule(time(11, 0), time(22, 0), time(23, 0))
    now = datetime(2026, 10, 6, 18, 30, 0, tzinfo=timezone.utc)  # 23:30 KHI
    status, message = sched.status(now)
    assert status == "closed"
    assert "11:00" in message


def test_can_take_orders_during_service():
    sched = Schedule(time(11, 0), time(22, 0), time(23, 0))
    assert sched.can_take_orders(datetime(2026, 10, 6, 9, 0, 0, tzinfo=timezone.utc)) is True


def test_can_take_orders_after_cutoff():
    sched = Schedule(time(11, 0), time(22, 0), time(23, 0))
    assert sched.can_take_orders(datetime(2026, 10, 6, 17, 30, 0, tzinfo=timezone.utc)) is False


def test_load_schedule_none_when_unset():
    assert load_schedule(None, None, None) is None
    assert load_schedule("11:00", None, "23:00") is None


def test_load_schedule_parses():
    sched = load_schedule("11:00", "21:30", "23:00")
    assert sched is not None
    assert sched.opening == time(11, 0)
    assert sched.last_order == time(21, 30)
    assert sched.closing == time(23, 0)


# ---- Enforcement in OrderService._move ---------------------------------

@pytest.fixture
def schedule():
    return Schedule(time(11, 0), time(22, 0), time(23, 0))


def test_validate_order_refused_before_open(db_url, menu, schedule, monkeypatch):
    svc = service_for(db_url, menu, schedule)
    order, _ = svc.create("k1")
    svc.add_item(order["id"], item_named(menu, "Chicken Supreme").id, 1, pizza_selections())
    freeze_utc(monkeypatch, 5)  # 10:00 KHI — before open
    with pytest.raises(DomainError) as exc:
        svc.transition(order["id"], S.VALIDATING_ORDER, "test")
    assert exc.value.code == "restaurant_closed"


def test_validate_order_ok_during_service(db_url, menu, schedule, monkeypatch):
    svc = service_for(db_url, menu, schedule)
    order, _ = svc.create("k2")
    svc.add_item(order["id"], item_named(menu, "Chicken Supreme").id, 1, pizza_selections())
    freeze_utc(monkeypatch, 9)  # 14:00 KHI — open
    result = svc.transition(order["id"], S.VALIDATING_ORDER, "test")
    assert result["state"] == S.VALIDATING_ORDER.value


def test_confirm_order_refused_after_cutoff(db_url, menu, schedule, monkeypatch):
    """Confirming after last-order cutoff raises restaurant_closed."""
    svc = service_for(db_url, menu, schedule)
    order, _ = svc.create("k3")
    svc.add_item(order["id"], item_named(menu, "Chicken Supreme").id, 1, pizza_selections())
    freeze_utc(monkeypatch, 9)  # open: reach a state from which CONFIRMED is a legal transition
    svc.transition(order["id"], S.VALIDATING_ORDER, "checkout")
    svc.transition(order["id"], S.HANDOFF_REQUIRED, "staff path to confirmation")
    freeze_utc(monkeypatch, 17, 30)  # 22:30 KHI — after cutoff
    with pytest.raises(DomainError) as exc:
        svc.transition(order["id"], S.CONFIRMED, "confirm")
    assert exc.value.code == "restaurant_closed"


def test_no_enforcement_when_schedule_is_none(db_url, menu, monkeypatch):
    svc = service_for(db_url, menu, None)
    order, _ = svc.create("k4")
    svc.add_item(order["id"], item_named(menu, "Chicken Supreme").id, 1, pizza_selections())
    freeze_utc(monkeypatch, 5)
    result = svc.transition(order["id"], S.VALIDATING_ORDER, "test")
    assert result["state"] == S.VALIDATING_ORDER.value


# ---- /schedule endpoint -----------------------------------------------

@pytest.fixture
def api_with_schedule(db_url):
    settings = Settings(_env_file=None, database_url=db_url, menu_path="data/menu.json",
                        restaurant_lat=ORIGIN[0], restaurant_lon=ORIGIN[1],
                        branches_path="tests/data_branches.json",
                        opening_time="11:00", last_order_time="22:00", closing_time="23:00")
    with TestClient(create_app(settings, models=[], geo=None)) as c:
        yield c


def test_schedule_endpoint_configured(api_with_schedule, monkeypatch):
    freeze_utc(monkeypatch, 9)
    resp = api_with_schedule.get("/schedule")
    assert resp.status_code == 200
    body = resp.json()
    assert body["configured"] is True
    assert body["status"] == "open"
    assert body["opening"] == "11:00"
    assert body["last_order"] == "22:00"
    assert body["closing"] == "23:00"


def test_schedule_endpoint_closed(api_with_schedule, monkeypatch):
    freeze_utc(monkeypatch, 17, 30)  # 22:30 KHI — after last-order cutoff
    resp = api_with_schedule.get("/schedule")
    assert resp.status_code == 200
    assert resp.json()["status"] == "after_cutoff"


# ---- Overnight schedule (crosses midnight) ----------------------------------

OVERNIGHT = Schedule(time(11, 0), time(3, 0), time(3, 0))  # 11:00–03:00 next day


@pytest.mark.parametrize("utc_hour,khi,status", [
    (4, "09:00", "closed"),   # before opening
    (6, "11:00", "open"),     # opening time
    (9, "14:00", "open"),     # afternoon
    (14, "19:00", "open"),    # evening
    (19, "00:00", "open"),    # midnight
    (21, "02:00", "open"),    # late night, still taking orders
    (21, "02:59", "open"),    # one minute before closing
    (22, "03:00", "closed"),  # closing time (exact)
])
def test_overnight_status(utc_hour, khi, status):
    now = datetime(2026, 10, 6, utc_hour, 0, 0, tzinfo=timezone.utc)
    assert OVERNIGHT.status(now)[0] == status, f"UTC {utc_hour:02d}:00 ({khi} KHI) should be {status}"


def test_overnight_with_after_cutoff_window():
    """When last_order < closing in an overnight schedule, there's an after_cutoff gap."""
    sched = Schedule(time(11, 0), time(2, 30), time(3, 0))
    # 02:00 KHI = 21:00 UTC → still taking orders
    assert sched.status(datetime(2026, 10, 6, 21, 0, 0, tzinfo=timezone.utc))[0] == "open"
    # 02:31 KHI = 21:31 UTC → after cutoff
    assert sched.status(datetime(2026, 10, 6, 21, 31, 0, tzinfo=timezone.utc))[0] == "after_cutoff"
    # 02:59 KHI = 21:59 UTC → still after cutoff
    assert sched.status(datetime(2026, 10, 6, 21, 59, 0, tzinfo=timezone.utc))[0] == "after_cutoff"
    # 03:00 KHI = 22:00 UTC → closed
    assert sched.status(datetime(2026, 10, 6, 22, 0, 0, tzinfo=timezone.utc))[0] == "closed"


def test_overnight_enforcement_frozen(menu, db_url, monkeypatch):
    """When the restaurant runs overnight hours, enforcement still blocks transitions while closed."""
    svc = service_for(db_url, menu, OVERNIGHT)
    order, _ = svc.create("overnight")
    svc.add_item(order["id"], item_named(menu, "Chicken Supreme").id, 1, pizza_selections())
    freeze_utc(monkeypatch, 4)  # 09:00 KHI — closed
    with pytest.raises(DomainError) as exc:
        svc.transition(order["id"], S.VALIDATING_ORDER, "test")
    assert exc.value.code == "restaurant_closed"


def test_schedule_endpoint_overnight(db_url):
    """The /schedule endpoint reflects overnight hours."""
    settings = Settings(_env_file=None, database_url=db_url, menu_path="data/menu.json",
                        restaurant_lat=ORIGIN[0], restaurant_lon=ORIGIN[1],
                        branches_path="tests/data_branches.json",
                        opening_time="11:00", last_order_time="03:00", closing_time="03:00")
    with TestClient(create_app(settings, models=[], geo=None)) as c:
        resp = c.get("/schedule")
        assert resp.status_code == 200
        body = resp.json()
        assert body["configured"] is True
        assert body["opening"] == "11:00"
        assert body["last_order"] == "03:00"
        assert body["closing"] == "03:00"
