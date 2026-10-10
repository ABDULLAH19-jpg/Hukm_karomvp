import threading
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.errors import DomainError
from app.states import TERMINAL, TRANSITIONS, OrderState, check_transition
from tests.conftest import item_named, pizza_selections

HAPPY_PATH = ["VALIDATING_ORDER", "COLLECTING_ADDRESS", "CHECKING_DELIVERY", "READY_FOR_CONFIRMATION",
              "CONFIRMED", "SUBMITTED_TO_RESTAURANT", "PREPARING", "READY", "COMPLETED"]


def new_order(client, key="call-1"):
    return client.post("/orders", headers={"Idempotency-Key": key}).json()


def add_pizza(client, order_id, menu, qty=1):
    body = {"item_id": item_named(menu, "Chicken Supreme").id, "quantity": qty,
            "selections": pizza_selections()}
    return client.post(f"/orders/{order_id}/items", json=body)


def go(client, order_id, state, reason=None):
    return client.post(f"/orders/{order_id}/transition", json={"to": state, "reason": reason})


def deliver(client, oid):
    """Address resolve, customer confirmation, then the zone check, the way a real checkout does it."""
    found = client.post(f"/orders/{oid}/address/resolve", json={"raw": "12 Street 5", "house": "12", "street": "5", "area": "DHA Phase 4"}).json()
    assert found["status"] == "confirm", found
    assert client.post(f"/orders/{oid}/address/confirm", json={"number": 1}).status_code == 200
    checked = client.post(f"/orders/{oid}/delivery/check").json()
    assert checked["outcome"] == "ready", checked
    return checked


def test_health_reports_menu_and_database(client):
    assert client.get("/health").json() == {"status": "ok", "menu_items": 103}


def test_duplicate_create_returns_same_order(client):
    first = client.post("/orders", headers={"Idempotency-Key": "k1"})
    second = client.post("/orders", headers={"Idempotency-Key": "k1"})
    other = client.post("/orders", headers={"Idempotency-Key": "k2"})
    assert (first.status_code, second.status_code) == (201, 200)
    assert first.json()["id"] == second.json()["id"] != other.json()["id"]


def test_concurrent_creates_with_one_key_make_one_order(client):
    ids = []
    def hit():
        ids.append(client.post("/orders", headers={"Idempotency-Key": "race"}).json()["id"])
    threads = [threading.Thread(target=hit) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(set(ids)) == 1


def test_client_cannot_supply_a_price(client, menu):
    order = new_order(client)
    body = {"item_id": item_named(menu, "Chicken Supreme").id, "quantity": 1, "price": 1,
            "selections": pizza_selections()}
    view = client.post(f"/orders/{order['id']}/items", json=body).json()
    assert view["subtotal"] == "1580.00"


def test_full_happy_path_with_history(client, menu):
    oid = new_order(client)["id"]
    assert add_pizza(client, oid, menu, qty=2).json()["state"] == "COLLECTING_ITEMS"
    assert go(client, oid, "VALIDATING_ORDER").status_code == 200
    deliver(client, oid)  # moves through COLLECTING_ADDRESS and CHECKING_DELIVERY to READY_FOR_CONFIRMATION
    for state in HAPPY_PATH[HAPPY_PATH.index("CONFIRMED"):]:
        resp = go(client, oid, state)
        assert resp.status_code == 200, resp.json()
    done = client.get(f"/orders/{oid}").json()
    assert (done["state"], done["total"]) == ("COMPLETED", "3160.00")
    trail = [e["to"] for e in client.get(f"/orders/{oid}/history").json()]
    assert trail == ["DRAFT", "COLLECTING_ITEMS", *HAPPY_PATH]


def test_invalid_transitions_are_rejected(client, menu):
    oid = new_order(client)["id"]
    assert go(client, oid, "CONFIRMED").json()["error"]["code"] == "invalid_transition"
    add_pizza(client, oid, menu)
    assert go(client, oid, "READY").status_code == 409


def test_cannot_validate_empty_order_or_skip_address(client, menu):
    oid = new_order(client)["id"]
    client.post(f"/orders/{oid}/items", json={"item_id": item_named(menu, "Chicken Supreme").id,
                                              "selections": pizza_selections()})
    client.delete(f"/orders/{oid}/items/{client.get(f'/orders/{oid}').json()['lines'][0]['line_id']}")
    assert go(client, oid, "VALIDATING_ORDER").json()["error"]["code"] == "empty_order"
    add_pizza(client, oid, menu)
    go(client, oid, "VALIDATING_ORDER")
    go(client, oid, "COLLECTING_ADDRESS")
    assert go(client, oid, "CHECKING_DELIVERY").json()["error"]["code"] == "address_required"


def test_cart_is_locked_after_validation_and_reopens_for_corrections(client, menu):
    oid = new_order(client)["id"]
    add_pizza(client, oid, menu)
    go(client, oid, "VALIDATING_ORDER")
    assert add_pizza(client, oid, menu).json()["error"]["code"] == "order_locked"
    reopened = go(client, oid, "COLLECTING_ITEMS").json()
    assert reopened["total"] is None
    assert add_pizza(client, oid, menu).json()["subtotal"] == "3160.00"


def test_quantity_update_and_remove(client, menu):
    oid = new_order(client)["id"]
    line = add_pizza(client, oid, menu).json()["lines"][0]["line_id"]
    assert client.patch(f"/orders/{oid}/items/{line}", json={"quantity": 4}).json()["subtotal"] == "6320.00"
    assert client.patch(f"/orders/{oid}/items/{line}", json={"quantity": 0}).status_code == 422
    assert client.delete(f"/orders/{oid}/items/{line}").json()["lines"] == []


def test_failure_states_are_terminal(client, menu):
    oid = new_order(client)["id"]
    add_pizza(client, oid, menu)
    assert go(client, oid, "CANCELLED", "customer hung up").status_code == 200
    assert go(client, oid, "COLLECTING_ITEMS").status_code == 409
    assert add_pizza(client, oid, menu).status_code == 409


def test_repeated_transition_request_is_a_noop(client, menu):
    oid = new_order(client)["id"]
    add_pizza(client, oid, menu)
    go(client, oid, "VALIDATING_ORDER")
    go(client, oid, "VALIDATING_ORDER")
    trail = [e["to"] for e in client.get(f"/orders/{oid}/history").json()]
    assert trail.count("VALIDATING_ORDER") == 1


def test_confirmation_catches_menu_price_drift_after_quote(client, menu, monkeypatch):
    oid = new_order(client)["id"]
    add_pizza(client, oid, menu)
    go(client, oid, "VALIDATING_ORDER")
    deliver(client, oid)
    assert client.get(f"/orders/{oid}").json()["total"] == "1580.00"

    live_menu = client.app.state.menu  # the menu the running app prices from
    pizza = live_menu.items[item_named(menu, "Chicken Supreme").id]
    dearer = tuple(replace(g, choices=tuple(replace(c, price=c.price + 50) if c.id == 1400158 else c
                                            for c in g.choices)) for g in pizza.groups)
    monkeypatch.setitem(live_menu.items, pizza.id, replace(pizza, groups=dearer))

    assert client.get(f"/orders/{oid}").json()["total"] == "1580.00"  # the quote is frozen
    assert go(client, oid, "CONFIRMED").json()["error"]["code"] == "price_changed"


def test_orders_survive_restart(settings, menu):
    with TestClient(create_app(settings)) as first:
        oid = new_order(first)["id"]
        add_pizza(first, oid, menu, qty=2)
        go(first, oid, "VALIDATING_ORDER")
    with TestClient(create_app(settings)) as second:  # new app, same database file
        again = second.get(f"/orders/{oid}").json()
        assert (again["state"], again["total"]) == ("VALIDATING_ORDER", "3160.00")
        assert second.post("/orders", headers={"Idempotency-Key": "call-1"}).json()["id"] == oid


def test_unknown_order_is_404(client):
    assert client.get("/orders/9999").status_code == 404


def test_startup_fails_on_broken_menu(settings, tmp_path):
    bad = tmp_path / "menu.json"
    bad.write_text("{}", encoding="utf-8")
    settings.menu_path = str(bad)
    with pytest.raises(Exception):
        with TestClient(create_app(settings)):
            pass


def test_transition_table_is_consistent():
    # OUT_OF_ZONE can recover, so it is not terminal. A rejected order is finished for good, so it is.
    assert TERMINAL == {OrderState.COMPLETED, OrderState.CANCELLED, OrderState.REJECTED, OrderState.FAILED}
    assert all(t in TRANSITIONS for targets in TRANSITIONS.values() for t in targets)
    with pytest.raises(DomainError):
        check_transition(OrderState.COMPLETED, OrderState.PREPARING)
