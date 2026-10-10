"""Vapi voice-channel webhook tests: tool-calls, transcript flows, evidence guards, call isolation."""
import json

from app.config import Settings, SecretStr
from app.api import create_app
from app.states import OrderState as S
from app.vapi import CallTranscripts, vapi_tool_definitions
from fastapi.testclient import TestClient
from tests.conftest import ORIGIN, PIZZA, item_named


# ---- helpers ----------------------------------------------------------------

def _tool_call_envelope(call_id: str, tools: list[dict]) -> dict:
    """Build a Vapi tool-calls message envelope."""
    tool_call_list = []
    for tc in tools:
        tool_call_list.append({"id": tc.get("id", f"tc_{tc['name']}"),
                               "name": tc["name"],
                               "parameters": tc.get("parameters", {})})
    return {"message": {"type": "tool-calls", "call": {"id": call_id},
                        "toolCallList": tool_call_list}}


def _transcript_event(call_id: str, transcript: str, role: str = "user") -> dict:
    return {"message": {"type": "transcript", "call": {"id": call_id},
                        "role": role, "transcript": transcript, "transcriptType": "final"}}


def _vapi_post(client: TestClient, body: dict) -> tuple[int, dict]:
    resp = client.post("/vapi/tool-calls", json=body)
    return resp.status_code, resp.json() if resp.content else {}


def _heard(client: TestClient, call_id: str) -> list[str]:
    """The caller's words as the evidence guard reads them, straight from the store the handler uses."""
    return CallTranscripts(client.app.state.sessions).heard(call_id)


def _orders_on_call(client: TestClient) -> list[dict]:
    """Every live order. A voice call always creates its order before running any tool."""
    return client.get("/orders/active").json()


def _selections(crust_words: str, size_words: str) -> list[dict]:
    """The two selections a Chicken Supreme needs, each quoting what the customer actually said.

    The guard looks for the quote as a contiguous run of the caller's words, so the two groups need
    their own phrases: one phrase cannot cover both unless the caller said it that way.
    """
    return [{"group_id": PIZZA["crust"], "choice_ids": [PIZZA["deep_pan"]], "customer_words": crust_words},
            {"group_id": PIZZA["size"], "choice_ids": [PIZZA["regular"]], "customer_words": size_words}]


# ---- tool-definition export -----------------------------------------------


def test_vapi_tool_definitions_are_valid_openai_format():
    defs = vapi_tool_definitions()
    assert len(defs) >= 19
    for d in defs:
        assert d["type"] == "function"
        fn = d["function"]
        assert "name" in fn
        assert "description" in fn
        params = fn["parameters"]
        assert params["type"] == "object"
        assert isinstance(params["properties"], dict)
        assert isinstance(params["required"], list)

    names = {d["function"]["name"] for d in defs}
    assert "add_to_cart" in names
    assert "confirm_order" in names
    assert "check_delivery_area" in names
    assert "check_open" in names

    # add_to_cart must have item_id, quantity, and selections
    add_to_cart = next(d["function"] for d in defs if d["function"]["name"] == "add_to_cart")
    props = add_to_cart["parameters"]["properties"]
    assert "item_id" in props
    assert "quantity" in props
    assert "selections" in props


# ---- happy path: full order via Vapi tool calls ---------------------------


def test_full_order_via_tool_calls(client, menu):
    call_id = "call_001"
    pizza_id = item_named(menu, "Chicken Supreme").id

    # Simulate customer speech
    _vapi_post(client, _transcript_event(call_id, "I want a chicken supreme pizza"))
    _vapi_post(client, _transcript_event(call_id, "deep pan"))
    _vapi_post(client, _transcript_event(call_id, "regular size with cheese topping"))

    # Search menu
    status, result = _vapi_post(client, _tool_call_envelope(call_id, [
        {"name": "search_menu", "parameters": {"query": "chicken supreme"}},
    ]))
    assert status == 200
    search = json.loads(result["results"][0]["result"])
    assert search["ok"] is True

    # Get menu item detail
    status, result = _vapi_post(client, _tool_call_envelope(call_id, [
        {"name": "get_menu_item", "parameters": {"item_id": pizza_id}},
    ]))
    detail = json.loads(result["results"][0]["result"])
    assert detail["ok"] is True

    # Add to cart using known option IDs from the conftest PIZZA dict
    status, result = _vapi_post(client, _tool_call_envelope(call_id, [{
        "name": "add_to_cart",
        "parameters": {"item_id": pizza_id, "quantity": 1,
                       "selections": _selections("deep pan", "regular")},
    }]))
    add_result = json.loads(result["results"][0]["result"])
    assert add_result["ok"] is True, f"add_to_cart failed: {add_result}"
    assert len(add_result["lines"]) >= 1

    # Create order (checkout)
    status, result = _vapi_post(client, _tool_call_envelope(call_id, [
        {"name": "create_order", "parameters": {}},
    ]))
    create = json.loads(result["results"][0]["result"])
    assert create["ok"] is True
    assert create["state"] == S.VALIDATING_ORDER.value

    # The call is bound to one live order, found through the API rather than a server-side map.
    live = _orders_on_call(client)
    assert [o["state"] for o in live] == [S.VALIDATING_ORDER.value]


# ---- customer choice evidence guard ----------------------------------------


def test_add_to_cart_guard_missing_customer_words(client, menu):
    call_id = "call_002"
    pizza_id = item_named(menu, "Chicken Supreme").id
    _vapi_post(client, _transcript_event(call_id, "pizza"))

    # Add to cart with empty customer_words — the transcript has only "pizza", not the option names
    status, result = _vapi_post(client, _tool_call_envelope(call_id, [{
        "name": "add_to_cart",
        "parameters": {"item_id": pizza_id, "quantity": 1,
                       "selections": _selections("", "")},
    }]))
    add_result = json.loads(result["results"][0]["result"])
    assert add_result["ok"] is False
    assert "not chosen" in add_result.get("message", "").lower()


# ---- confirm_order evidence guard ------------------------------------------


def test_confirm_order_guard_no_yes(client):
    call_id = "call_003"
    # The customer has placed an order. The Vapi assistant should not confirm without a yes.
    _vapi_post(client, _transcript_event(call_id, "place order"))

    # Try to confirm without a yes in the transcript
    status, result = _vapi_post(client, _tool_call_envelope(call_id, [
        {"name": "confirm_order", "parameters": {}},
    ]))
    confirm = json.loads(result["results"][0]["result"])
    # The confirm guard OR the wrong-state guard can fire. Either way, it should not succeed.
    assert confirm["ok"] is False


# ---- call isolation --------------------------------------------------------


def test_different_calls_get_different_orders(client):
    a_id, b_id = "call_a", "call_b"

    _vapi_post(client, _transcript_event(a_id, "pizza"))
    _vapi_post(client, _transcript_event(b_id, "burger"))
    _vapi_post(client, _tool_call_envelope(a_id, [{"name": "search_menu", "parameters": {"query": "chicken supreme"}}]))
    _vapi_post(client, _tool_call_envelope(b_id, [{"name": "search_menu", "parameters": {"query": "zinger burger"}}]))

    live = _orders_on_call(client)
    assert len(live) == 2, f"each call needs its own order, got {live}"
    assert len({o["id"] for o in live}) == 2

    # What one caller said is never evidence for another caller's order.
    assert _heard(client, a_id) == ["pizza"]
    assert _heard(client, b_id) == ["burger"]


def test_one_callers_words_are_not_evidence_on_another_call(client, menu):
    """The guard reads this call's words and nothing else."""
    pizza_id = item_named(menu, "Chicken Supreme").id
    _vapi_post(client, _transcript_event("call_asked", "I want a deep pan pizza with regular size"))
    _vapi_post(client, _transcript_event("call_silent", "hello"))

    # call_silent never said "deep pan", so the same call must be refused for it.
    _, result = _vapi_post(client, _tool_call_envelope("call_silent", [{
        "name": "add_to_cart",
        "parameters": {"item_id": pizza_id, "quantity": 1,
                       "selections": _selections("deep pan", "regular size")},
    }]))
    assert json.loads(result["results"][0]["result"])["ok"] is False

    # The call that did say it is served.
    _, result = _vapi_post(client, _tool_call_envelope("call_asked", [{
        "name": "add_to_cart",
        "parameters": {"item_id": pizza_id, "quantity": 1,
                       "selections": _selections("deep pan", "regular size")},
    }]))
    assert json.loads(result["results"][0]["result"])["ok"] is True


# ---- surviving a restart ---------------------------------------------------


def test_a_replayed_call_lands_on_the_same_order(settings, menu):
    """A retry, a duplicated webhook, or a restart must not make a second order for one call."""
    with TestClient(create_app(settings, models=[], geo=None)) as first:
        _vapi_post(first, _tool_call_envelope("call_restart", [
            {"name": "search_menu", "parameters": {"query": "chicken supreme"}}]))
        before = _orders_on_call(first)
    assert len(before) == 1

    with TestClient(create_app(settings, models=[], geo=None)) as second:  # new app, same database
        _vapi_post(second, _tool_call_envelope("call_restart", [
            {"name": "search_menu", "parameters": {"query": "chicken supreme"}}]))
        after = _orders_on_call(second)

    assert [o["id"] for o in after] == [o["id"] for o in before]
    assert [o["number"] for o in after] == [o["number"] for o in before]


def test_the_callers_words_survive_a_restart(settings, menu):
    """The evidence guard is only as good as the store behind it. In memory, a restart erased it."""
    pizza_id = item_named(menu, "Chicken Supreme").id
    with TestClient(create_app(settings, models=[], geo=None)) as first:
        _vapi_post(first, _transcript_event("call_words", "deep pan, regular size please"))
    with TestClient(create_app(settings, models=[], geo=None)) as second:
        assert _heard(second, "call_words") == ["deep pan, regular size please"]
        _, result = _vapi_post(second, _tool_call_envelope("call_words", [{
            "name": "add_to_cart",
            "parameters": {"item_id": pizza_id, "quantity": 1,
                           "selections": _selections("deep pan", "regular size")},
        }]))
    assert json.loads(result["results"][0]["result"])["ok"] is True, "the guard lost the caller's words on restart"


# ---- auth ------------------------------------------------------------------


def test_vapi_auth_missing_secret_allows_when_unconfigured(client):
    """Without a secret configured, requests are allowed (dev mode)."""
    status, result = _vapi_post(client, _tool_call_envelope("call_dev", [
        {"name": "get_cart", "parameters": {}},
    ]))
    assert status == 200


def test_vapi_auth_rejects_wrong_secret(db_url):
    settings = Settings(_env_file=None, database_url=db_url, menu_path="data/menu.json",
                        restaurant_lat=ORIGIN[0], restaurant_lon=ORIGIN[1],
                        branches_path="tests/data_branches.json",
                        vapi_webhook_secret=SecretStr("my-secret"))
    with TestClient(create_app(settings, models=[], geo=None)) as c:
        # No auth header
        resp = c.post("/vapi/tool-calls", json=_tool_call_envelope("c", [
            {"name": "get_cart", "parameters": {}},
        ]))
        assert resp.status_code == 401

        # Wrong auth header
        resp = c.post("/vapi/tool-calls", json=_tool_call_envelope("c", [
            {"name": "get_cart", "parameters": {}},
        ]), headers={"Authorization": "Bearer wrong"})
        assert resp.status_code == 401

        # Correct auth header
        resp = c.post("/vapi/tool-calls", json=_tool_call_envelope("c", [
            {"name": "get_cart", "parameters": {}},
        ]), headers={"Authorization": "Bearer my-secret"})
        assert resp.status_code == 200


# ---- unknown tool ----------------------------------------------------------


def test_unknown_tool_returns_error(client):
    call_id = "call_unknown"
    status, result = _vapi_post(client, _tool_call_envelope(call_id, [
        {"name": "nonexistent_tool", "parameters": {}},
    ]))
    assert status == 200
    r = json.loads(result["results"][0]["result"])
    assert r["ok"] is False
    assert r["error"] == "unknown_tool"


# ---- end-of-call cleanup ---------------------------------------------------


def test_end_of_call_drops_the_words_but_not_the_order(client):
    call_id = "call_end"
    _vapi_post(client, _transcript_event(call_id, "hi there"))
    _vapi_post(client, _tool_call_envelope(call_id, [{"name": "get_cart", "parameters": {}}]))
    assert _heard(client, call_id) == ["hi there"]

    _vapi_post(client, {"message": {"type": "end-of-call-report", "call": {"id": call_id}}})
    assert _heard(client, call_id) == []
    # The order the call created is still there: only the customer's speech is no longer needed.
    assert len(_orders_on_call(client)) == 1
