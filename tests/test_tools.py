import logging

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.db import make_session_factory
from app.orders import OrderService
from app.tools import build_tools
from tests.conftest import PIZZA, item_named

PIZZA_SEL = [{"group_id": PIZZA["crust"], "choice_ids": [PIZZA["deep_pan"]]},
             {"group_id": PIZZA["size"], "choice_ids": [PIZZA["regular"]]},
             {"group_id": PIZZA["topping"], "choice_ids": [PIZZA["cheese_regular"]]}]


@pytest.fixture
def service(menu, db_url):
    return OrderService(make_session_factory(db_url), menu)


@pytest.fixture
def tools(service, delivery_for):
    order, _ = service.create("tool-test")
    named = {t.name: t for t in build_tools(service, order["id"], delivery=delivery_for(service))}
    named["_order_id"] = order["id"]
    return named


def call(tools, name, **args):
    return tools[name].invoke(args)


def test_tool_set_matches_the_plan(tools):
    expected = {"search_menu", "get_menu_item", "check_availability", "add_to_cart", "update_cart_item",
                "remove_cart_item", "get_cart", "calculate_total", "create_order", "resolve_address",
                "confirm_address", "check_delivery_zone", "check_delivery_area", "choose_pickup",
                "reopen_order", "confirm_order", "cancel_order", "get_order_status", "check_open", "handoff_to_staff"}
    assert expected == {k for k in tools if k != "_order_id"}


def test_no_tool_lets_the_model_set_a_price_or_pick_an_order(tools):
    for name, tool in tools.items():
        if name == "_order_id":
            continue
        fields = set(tool.args_schema.model_fields)
        assert not {"price", "total", "unit_price", "order_id"} & fields, name


def test_search_only_returns_real_items(tools, menu):
    found = call(tools, "search_menu", query="peri crunch")["items"]
    assert found and all(menu.get(i["item_id"]) for i in found)
    assert call(tools, "search_menu", query="unicorn truffle")["items"] == []


def test_search_shows_real_required_options_for_top_results(tools):
    top = call(tools, "search_menu", query="coke")["items"][0]
    assert top["name"] == "Coke"
    names = {c for g in top["required_options"] for c in g["choices"]}
    assert {"500 Ml Coke", "350 Ml Coke"} <= names and "Diet" not in " ".join(names)


def test_get_menu_item_exposes_required_options_and_dependencies(tools, menu):
    detail = call(tools, "get_menu_item", item_id=item_named(menu, "Chicken Supreme").id)["item"]
    size = next(g for g in detail["option_groups"] if g["group_id"] == PIZZA["size"])
    assert size["required"] and size["only_after_group"] == PIZZA["crust"]


def test_refusals_come_back_as_data_the_model_can_act_on(tools, menu):
    wings = item_named(menu, "Oven Baked Wings").id
    missing = call(tools, "add_to_cart", item_id=wings, quantity=1)
    assert missing["ok"] is False and missing["error"] == "missing_required_option"
    assert call(tools, "add_to_cart", item_id=424242)["error"] == "unknown_item"
    assert call(tools, "get_cart")["lines"] == []


def test_full_order_through_tools_only(tools, menu):
    pizza = item_named(menu, "Chicken Supreme").id
    added = call(tools, "add_to_cart", item_id=pizza, quantity=2, selections=PIZZA_SEL)
    assert added["ok"] and added["subtotal"] == "3160.00"
    line = added["lines"][0]["line_id"]
    assert call(tools, "update_cart_item", line_id=line, quantity=1)["subtotal"] == "1580.00"

    early = call(tools, "confirm_order")  # state machine blocks skipping steps
    assert early["ok"] is False and early["error"] == "invalid_transition"

    assert call(tools, "create_order")["state"] == "VALIDATING_ORDER"
    found = call(tools, "resolve_address", raw="phase 4 mein 12 street 5", house="12", street="5", area="DHA Phase 4")
    assert found["status"] == "confirm" and found["candidates"][0]["label"].startswith("House 12")
    assert "lat" not in str(found) and "lon" not in str(found)  # the model never handles coordinates
    assert call(tools, "confirm_address", number=1)["confirmed"] is True
    zone = call(tools, "check_delivery_zone")
    assert zone["outcome"] == "ready" and zone["state"] == "READY_FOR_CONFIRMATION"
    confirmed = call(tools, "confirm_order")
    assert (confirmed["state"], confirmed["total"]) == ("CONFIRMED", "1580.00")
    assert call(tools, "get_order_status")["order_number"].startswith("HK-")


def test_zone_check_cannot_run_before_the_customer_confirms_a_place(tools, menu):
    call(tools, "add_to_cart", item_id=item_named(menu, "Chicken Supreme").id, selections=PIZZA_SEL)
    call(tools, "create_order")
    call(tools, "resolve_address", raw="x", area="DHA Phase 4")
    assert call(tools, "check_delivery_zone")["error"] == "address_not_confirmed"
    assert call(tools, "confirm_address", number=7)["error"] == "no_address_to_confirm"


def test_out_of_zone_address_ends_the_order_with_a_clear_message(tools, menu):
    call(tools, "add_to_cart", item_id=item_named(menu, "Chicken Supreme").id, selections=PIZZA_SEL)
    call(tools, "create_order")
    call(tools, "resolve_address", raw="phase 8", area="phase 8", street="9")
    call(tools, "confirm_address", number=1)
    zone = call(tools, "check_delivery_zone")
    assert zone["outcome"] == "out_of_zone" and "3 km" in zone["tell_customer"]
    assert call(tools, "confirm_order")["error"] == "invalid_transition"


def test_address_tools_refuse_when_delivery_is_not_configured(service):
    order, _ = service.create("no-delivery")
    bare = {t.name: t for t in build_tools(service, order["id"])}
    assert bare["resolve_address"].invoke({"raw": "x"})["error"] == "delivery_not_configured"
    assert bare["check_delivery_zone"].invoke({})["error"] == "delivery_not_configured"


def test_customer_can_change_items_after_checkout_starts(tools, menu):
    pizza = item_named(menu, "Chicken Supreme").id
    call(tools, "add_to_cart", item_id=pizza, selections=PIZZA_SEL)
    call(tools, "create_order")
    assert call(tools, "add_to_cart", item_id=pizza, selections=PIZZA_SEL)["error"] == "order_locked"
    assert call(tools, "reopen_order")["state"] == "COLLECTING_ITEMS"
    assert call(tools, "add_to_cart", item_id=pizza, selections=PIZZA_SEL)["subtotal"] == "3160.00"


def test_handoff_and_cancel(service, tools):
    call(tools, "add_to_cart", item_id=item_named(service.menu, "Chicken Supreme").id, selections=PIZZA_SEL)
    assert call(tools, "handoff_to_staff", reason="customer asked for a person")["state"] == "HANDOFF_REQUIRED"
    assert call(tools, "cancel_order")["state"] == "CANCELLED"


def test_tool_sets_are_isolated_per_order(service):
    a, _ = service.create("a")
    b, _ = service.create("b")
    tools_a = {t.name: t for t in build_tools(service, a["id"])}
    tools_b = {t.name: t for t in build_tools(service, b["id"])}
    tools_a["add_to_cart"].invoke({"item_id": item_named(service.menu, "Chicken Supreme").id,
                                   "selections": PIZZA_SEL})
    assert tools_b["get_cart"].invoke({})["lines"] == []


def test_every_tool_execution_is_logged(tools, caplog):
    with caplog.at_level(logging.INFO, logger="hukm"):
        call(tools, "search_menu", query="burger")
        call(tools, "add_to_cart", item_id=424242)
    logged = [r for r in caplog.records if r.getMessage() == "tool_call"]
    assert [(r.tool, r.ok) for r in logged] == [("search_menu", True), ("add_to_cart", False)]
    assert logged[1].error == "unknown_item" and logged[1].order_id == tools["_order_id"]


def test_unexpected_crash_is_contained_and_logged(tools, service, caplog, monkeypatch):
    monkeypatch.setattr(service, "get", lambda *_: 1 / 0)
    with caplog.at_level(logging.INFO, logger="hukm"):
        result = call(tools, "get_cart")
    assert result["ok"] is False and result["error"] == "internal_error"
    assert "division" not in result["message"]
    assert any(r.getMessage() == "tool_crash" for r in caplog.records)


def test_api_hides_internal_errors_and_tags_requests(settings, monkeypatch):
    app = create_app(settings)
    with TestClient(app, raise_server_exceptions=False) as client:
        monkeypatch.setattr(app.state.orders, "get", lambda *_: 1 / 0)
        resp = client.get("/orders/1", headers={"X-Request-ID": "abc123"})
        assert resp.status_code == 500 and resp.headers["X-Request-ID"] == "abc123"
        body = resp.json()["error"]
        assert body["code"] == "internal_error" and body["request_id"] == "abc123"
        assert "division" not in resp.text and "Traceback" not in resp.text
        bad = client.post("/orders/1/items", json={"item_id": "x"})
        assert bad.status_code == 422 and bad.json()["error"]["code"] == "invalid_request"
