"""The kitchen board: who may appear on it, and that every button it offers is a move the order can make.

Two bugs this covers. Orders the customer had not confirmed were showing up in front of the kitchen
(the screen had its own fallback column), and the columns and buttons lived in the frontend where the
server could not check them. Both now come from app/states.py and are asserted here.
"""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.db import make_session_factory
from app.orders import COMPLETED_WINDOW_H
from app.states import (BOARD_STATES, CONVERSATION_STATES, KITCHEN_ACTIONS, KITCHEN_COLUMNS, TERMINAL,
                        TRANSITIONS, OrderState)
from tests.conftest import item_named, pizza_selections

IN_ZONE = {"raw": "12 Street 5", "house": "12", "street": "5", "area": "DHA Phase 4"}
OUT_OF_ZONE = {"raw": "Street 9 DHA Phase 8", "street": "9", "area": "DHA Phase 8"}


# ---- the definition itself -------------------------------------------------
# These run without a client: they check the tables the server serves, so a state or a button added
# without thinking about the board fails here rather than in front of the kitchen.

def test_every_board_action_is_a_legal_transition():
    """A button whose target the state machine refuses is a dead control on a live screen."""
    for state, actions in KITCHEN_ACTIONS.items():
        for label, target, _tone in actions:
            assert target in TRANSITIONS[state], f"{state.value} -> {target.value} ({label}) is not allowed"


def test_every_non_terminal_board_state_has_an_action():
    """A card the kitchen can neither work on nor finish is a dead end."""
    for state in BOARD_STATES - TERMINAL:
        assert KITCHEN_ACTIONS.get(state), f"{state.value} sits on the board with nothing to do"


def test_actions_belong_to_states_that_are_on_the_board():
    for state in KITCHEN_ACTIONS:
        assert state in BOARD_STATES, f"{state.value} offers actions but never appears on the board"


def test_no_column_shows_the_same_state_twice():
    seen: dict[OrderState, str] = {}
    for key, _title, states in KITCHEN_COLUMNS:
        for state in states:
            assert state not in seen, f"{state.value} is in both {seen.get(state)} and {key}"
            seen[state] = key


def test_conversation_states_stay_off_the_board():
    assert not (BOARD_STATES & CONVERSATION_STATES)


def test_every_state_is_classified():
    covered = BOARD_STATES | CONVERSATION_STATES | TERMINAL
    missing = sorted(s.value for s in set(OrderState) - covered)
    assert covered == set(OrderState), f"these states are in no group: {missing}"


# ---- the endpoint ----------------------------------------------------------

def _new(client, key):
    return client.post("/orders", headers={"Idempotency-Key": key}).json()


def _add(client, oid, menu):
    return client.post(f"/orders/{oid}/items", json={"item_id": item_named(menu, "Chicken Supreme").id,
                                                     "quantity": 1, "selections": pizza_selections()})


def _go(client, oid, state, reason=None):
    return client.post(f"/orders/{oid}/transition", json={"to": state, "reason": reason})


def _board(client):
    return client.get("/orders/board").json()


def _column_of(board, state):
    return next(c["key"] for c in board["columns"] if state in c["states"])


def _parked(client, key, target, menu):
    """Drive a fresh order to `target` the way a real checkout does, and return its id."""
    oid = _new(client, key)["id"]
    if target == "DRAFT":
        return oid
    _add(client, oid, menu)
    if target == "COLLECTING_ITEMS":
        return oid
    _go(client, oid, "VALIDATING_ORDER")
    if target == "VALIDATING_ORDER":
        return oid
    _go(client, oid, "COLLECTING_ADDRESS")
    if target == "COLLECTING_ADDRESS":
        return oid
    fields = OUT_OF_ZONE if target in ("OUT_OF_ZONE", "PICKUP_SELECTED") else IN_ZONE
    found = client.post(f"/orders/{oid}/address/resolve", json=fields).json()
    assert found["status"] == "confirm", found
    assert client.post(f"/orders/{oid}/address/confirm", json={"number": 1}).status_code == 200
    if target == "CHECKING_DELIVERY":
        assert _go(client, oid, "CHECKING_DELIVERY").status_code == 200
        return oid
    checked = client.post(f"/orders/{oid}/delivery/check").json()
    if target == "READY_FOR_CONFIRMATION":
        assert checked["outcome"] == "ready", checked
        return oid
    assert checked["outcome"] == "out_of_zone", checked
    if target == "OUT_OF_ZONE":
        return oid
    assert _go(client, oid, "PICKUP_SELECTED").status_code == 200
    return oid


@pytest.mark.parametrize("state", sorted(s.value for s in CONVERSATION_STATES))
def test_a_live_conversation_never_reaches_the_board(client, menu, state):
    """Every one of these is a customer still deciding. The kitchen must not see any of them."""
    oid = _parked(client, f"convo-{state}", state, menu)
    assert client.get(f"/orders/{oid}").json()["state"] == state, "the order is not where the test meant to park it"
    board = _board(client)
    assert board["orders"] == [], f"a {state} order reached the kitchen: {board['orders']}"


def test_the_columns_and_the_window_come_from_the_server(client):
    board = _board(client)
    assert [(c["key"], c["title"]) for c in board["columns"]] == [(k, t) for k, t, _ in KITCHEN_COLUMNS]
    assert board["completed_window_h"] == COMPLETED_WINDOW_H
    assert set(board["actions"]) == {s.value for s in KITCHEN_ACTIONS}


def test_a_confirmed_order_walks_the_board_using_the_boards_own_buttons(client, menu):
    """The board's buttons, pressed in order, take an order from confirmed to completed."""
    oid = _parked(client, "walk", "READY_FOR_CONFIRMATION", menu)
    assert _go(client, oid, "CONFIRMED").status_code == 200
    assert [o["id"] for o in _board(client)["orders"]] == [oid]

    columns_seen = []
    for _ in range(8):
        board = _board(client)
        card = next((o for o in board["orders"] if o["id"] == oid), None)
        assert card is not None, "the order fell off the board before it was finished"
        columns_seen.append(_column_of(board, card["state"]))
        actions = board["actions"].get(card["state"], [])
        if not actions:
            break
        resp = client.post(f"/orders/{oid}/transition", json={"to": actions[0]["to"], "reason": "board button"})
        assert resp.status_code == 200, resp.json()
    else:
        pytest.fail("the board never settled at a state with no actions")

    assert columns_seen == ["new", "new", "preparing", "ready", "completed"]
    assert client.get(f"/orders/{oid}").json()["state"] == "COMPLETED"
    # It stays on the board after that, so the next refresh still shows what just went out.
    assert [o["id"] for o in _board(client)["orders"]] == [oid]


def test_a_handoff_card_carries_its_reason_and_a_way_out(client, menu):
    oid = _parked(client, "handoff", "READY_FOR_CONFIRMATION", menu)
    _go(client, oid, "CONFIRMED")
    assert _go(client, oid, "HANDOFF_REQUIRED", "customer asked for a person").status_code == 200

    board = _board(client)
    card = next(o for o in board["orders"] if o["id"] == oid)
    assert card["note"] == "customer asked for a person"
    assert _column_of(board, "HANDOFF_REQUIRED") == "handoff"
    assert {a["to"] for a in board["actions"]["HANDOFF_REQUIRED"]} == {
        "COLLECTING_ITEMS", "CONFIRMED", "CANCELLED", "FAILED"}


def test_the_kitchen_can_reject_a_confirmed_order(client, menu):
    oid = _parked(client, "reject", "READY_FOR_CONFIRMATION", menu)
    _go(client, oid, "CONFIRMED")
    _go(client, oid, "SUBMITTED_TO_RESTAURANT")

    reject = next(a for a in _board(client)["actions"]["SUBMITTED_TO_RESTAURANT"] if a["to"] == "REJECTED")
    resp = client.post(f"/orders/{oid}/transition", json={"to": reject["to"], "reason": "out of stock"})
    assert resp.status_code == 200, resp.json()

    board = _board(client)
    assert _column_of(board, "REJECTED") == "rejected"
    assert client.get(f"/orders/{oid}").json()["state"] == "REJECTED"
    # Rejected is a finished order, so the voice feed stops offering it.
    assert not any(o["id"] == oid for o in client.get("/orders/active").json())


def test_a_completed_order_leaves_the_board_after_the_window(client, menu, settings):
    oid = _parked(client, "window", "READY_FOR_CONFIRMATION", menu)
    _go(client, oid, "CONFIRMED")
    for state in ("SUBMITTED_TO_RESTAURANT", "PREPARING", "READY", "COMPLETED"):
        assert _go(client, oid, state).status_code == 200
    assert any(o["id"] == oid for o in _board(client)["orders"])

    aged = datetime.now(timezone.utc) - timedelta(hours=COMPLETED_WINDOW_H + 1)
    sessions = make_session_factory(settings.database_url)  # same database the app is serving from
    with sessions.begin() as s:
        s.execute(text("UPDATE orders SET updated_at = :when WHERE id = :id"), {"when": aged, "id": oid})

    assert not any(o["id"] == oid for o in _board(client)["orders"])
