"""LangChain tools: the only way the model touches an order. LLM -> tool -> service -> database.

Each tool set is bound to one order when it is built, so the model never supplies an order id
and cannot reach another customer's order. No tool accepts a price. Business refusals come back
as {"ok": false, "error": code, "message": ...} so the model can recover instead of crashing.
"""
import time
from typing import Callable

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from app.delivery import AddressFields, DeliveryService
from app.errors import DomainError
from app.evidence import check_confirm, check_customer_choices, check_place_pick
from app.logging_setup import logger
from app.menu import MenuItem
from app.orders import OrderService
from app.states import OrderState

S = OrderState


class Selection(BaseModel):
    group_id: int = Field(description="id of the option group, from get_menu_item")
    choice_ids: list[int] = Field(description="ids of the chosen choices in that group")
    customer_words: str = Field(default="", description="the customer's own words that chose these choices, copied "
                                "exactly from their messages. Never your own words, never the item name. "
                                "If they have not chosen, do not call add_to_cart: ask them.")


class Query(BaseModel):
    query: str = Field(description="what the customer asked for, e.g. 'peri crunch' or 'zinger burger'")


class ItemId(BaseModel):
    item_id: int = Field(description="menu item id from search_menu")


class AddToCart(BaseModel):
    item_id: int = Field(description="menu item id from search_menu")
    quantity: int = Field(default=1, description="how many, 1 to 20")
    selections: list[Selection] = Field(default_factory=list,
                                        description="the customer's option choices, required ones included")


class UpdateCartItem(BaseModel):
    line_id: str = Field(description="cart line id from get_cart")
    quantity: int = Field(description="new quantity, 1 to 20")


class LineId(BaseModel):
    line_id: str = Field(description="cart line id from get_cart")


class AddressInput(BaseModel):
    raw: str = Field(description="the address exactly as the customer said it, in their words")
    house: str = Field(default="", description="house or plot number, e.g. '223' or '12-A'")
    street: str = Field(default="", description="street name or number")
    block: str = Field(default="", description="block, sector or lane, e.g. 'FF' or 'Y Block'")
    area: str = Field(default="", description="phase, society or neighbourhood, e.g. 'DHA Phase 5'")
    landmark: str = Field(default="", description="a nearby landmark the customer mentioned, e.g. 'Rafi Mosque'")


class CandidateNumber(BaseModel):
    number: int = Field(description="the number of the place the customer confirmed, from resolve_address")


class NoArgs(BaseModel):
    pass


class Reason(BaseModel):
    reason: str = Field(default="customer needs a person", description="why a person is needed, one short line")


def _item_summary(item: MenuItem) -> dict:
    return {"item_id": item.id, "name": item.name, "price": str(item.price), "category": item.category,
            "available": item.available}


def _item_detail(item: MenuItem) -> dict:
    out = _item_summary(item)
    out["description"] = item.description
    out["option_groups"] = [{
        "group_id": g.id, "name": g.name, "required": g.required, "min": g.min_selections,
        "max": g.max_selections, "price_behavior": g.price_behavior.value,
        "only_after_group": g.linked_option_id,  # this group applies only once a choice in that group is picked
        "choices": [{"choice_id": c.id, "name": c.name, "price": str(c.price),
                     "only_with_choice": c.linked_choice_id} for c in g.choices],
    } for g in item.groups]
    return out


def build_tools(service: OrderService, order_id: int, delivery: DeliveryService | None = None,
                heard: Callable[[], list[str]] | None = None) -> list[StructuredTool]:
    """`heard` returns the customer's recent messages. When given, every option choice must be backed by them."""
    menu = service.menu

    # Address fields are customer PII and must not reach the logs.
    _PII_ARGS = {"raw", "house", "street", "block", "area", "landmark"}

    def _log_args(kwargs: dict) -> dict:
        return {k: ("<redacted>" if k in _PII_ARGS else v) for k, v in kwargs.items()}

    def guarded(name: str, fn: Callable[..., dict]) -> Callable[..., dict]:
        def run(**kwargs) -> dict:
            started = time.perf_counter()
            try:
                result = {"ok": True, **fn(**kwargs)}
                code = None
            except DomainError as exc:
                result, code = {"ok": False, "error": exc.code, "message": exc.message}, exc.code
            except Exception:
                logger.exception("tool_crash", extra={"tool": name, "order_id": order_id})
                result, code = {"ok": False, "error": "internal_error",
                                "message": "that did not work, try again or hand off to staff"}, "internal_error"
            logger.info("tool_call", extra={"tool": name, "order_id": order_id, "tool_args": _log_args(kwargs),
                                            "ok": result["ok"], "error": code,
                                            "ms": round((time.perf_counter() - started) * 1000)})
            return result
        return run

    def cart_view() -> dict:
        view = service.get(order_id)
        return {"state": view["state"], "lines": view["lines"], "subtotal": view["subtotal"],
                "delivery_fee": view["delivery_fee"], "total": view["total"], "issues": view["issues"]}

    def search_menu(query: str) -> dict:
        found = menu.search(query, 8)
        items = []
        for rank, item in enumerate(found):
            entry = _item_summary(item)
            if rank < 3:  # real option names up front, so the model never has to guess them
                entry["required_options"] = [
                    {"group_id": g.id, "name": g.name, "choices": sorted({c.name for c in g.choices})[:6]}
                    for g in item.groups if g.required]
            items.append(entry)
        return {"items": items}

    def get_menu_item(item_id: int) -> dict:
        item = menu.get(item_id)
        if item is None:
            raise DomainError("unknown_item", f"item {item_id} is not on the menu")
        return {"item": _item_detail(item)}

    def check_availability(item_id: int) -> dict:
        item = menu.get(item_id)
        if item is None:
            raise DomainError("unknown_item", f"item {item_id} is not on the menu")
        return {"item_id": item.id, "name": item.name, "available": item.available}

    def add_to_cart(item_id: int, quantity: int = 1, selections: list[Selection] | None = None) -> dict:
        chosen, evidence = {}, []
        for sel in selections or []:
            sel = Selection.model_validate(sel)
            chosen.setdefault(sel.group_id, []).extend(sel.choice_ids)
            evidence.append((sel.group_id, sel.choice_ids, sel.customer_words))
        item = menu.get(item_id)
        if heard is not None and item is not None:
            check_customer_choices(item, evidence, heard())
        service.add_item(order_id, item_id, quantity, chosen)
        return cart_view()

    def update_cart_item(line_id: str, quantity: int) -> dict:
        service.set_quantity(order_id, line_id, quantity)
        return cart_view()

    def remove_cart_item(line_id: str) -> dict:
        service.remove_item(order_id, line_id)
        return cart_view()

    def get_cart() -> dict:
        return cart_view()

    def calculate_total() -> dict:
        return cart_view()

    def create_order() -> dict:
        """Lock the cart and quote it. The customer then confirms the quote."""
        service.transition(order_id, S.VALIDATING_ORDER, "model requested checkout")
        return cart_view()

    def resolve_address(raw: str, house: str = "", street: str = "", block: str = "", area: str = "",
                        landmark: str = "") -> dict:
        if delivery is None:
            raise DomainError("delivery_not_configured", "delivery checks are not set up, hand off to staff")
        return delivery.resolve(order_id, AddressFields(raw=raw, house=house, street=street, block=block,
                                                        area=area, landmark=landmark))

    def confirm_address(number: int) -> dict:
        if heard is not None:
            texts = heard()
            check_place_pick(service.address_candidate_labels(order_id), number, texts[-1] if texts else "")
        view = service.confirm_address(order_id, number)
        return {"address": view["address"], "confirmed": True}

    def check_delivery_zone() -> dict:
        if delivery is None:
            raise DomainError("delivery_not_configured", "delivery checks are not set up, hand off to staff")
        result = delivery.check(order_id)
        return {**result, **cart_view()}

    def check_delivery_area(query: str) -> dict:
        if delivery is None:
            raise DomainError("delivery_not_configured", "delivery checks are not set up, hand off to staff")
        return delivery.check_area(query)

    def reopen_order() -> dict:
        service.transition(order_id, S.COLLECTING_ITEMS, "customer wants to change the order")
        return cart_view()

    def choose_pickup() -> dict:
        service.transition(order_id, S.PICKUP_SELECTED, "customer chose pickup after delivery was unavailable")
        return cart_view()

    def confirm_order() -> dict:
        if heard is not None:
            texts = heard()
            check_confirm(texts[-1] if texts else "")
        service.transition(order_id, S.CONFIRMED, "customer confirmed")
        return cart_view()

    def cancel_order() -> dict:
        service.transition(order_id, S.CANCELLED, "customer cancelled")
        return {"state": S.CANCELLED.value}

    def get_order_status() -> dict:
        view = service.get(order_id)
        return {"order_number": view["number"], "state": view["state"], "total": view["total"]}

    def check_open() -> dict:
        if service.schedule is None:
            return {"status": "open", "message": "We are open and taking orders."}
        status, message = service.schedule.status()
        return {"status": status, "message": message}

    def handoff_to_staff(reason: str = "customer needs a person") -> dict:
        service.transition(order_id, S.HANDOFF_REQUIRED, reason[:300])
        return {"state": S.HANDOFF_REQUIRED.value, "tell_customer": "a team member will take over"}

    specs = [
        ("search_menu", search_menu, Query, "Find menu items by name or category. Only items returned here exist."),
        ("get_menu_item", get_menu_item, ItemId,
         "Get one item's option groups, which are required, and their choices. Call before add_to_cart for items with options."),
        ("check_availability", check_availability, ItemId, "Check whether an item can be ordered right now."),
        ("add_to_cart", add_to_cart, AddToCart,
         "Add an item with the customer's option choices. Prices are set by the server, never by you."),
        ("update_cart_item", update_cart_item, UpdateCartItem, "Change the quantity of an existing cart line."),
        ("remove_cart_item", remove_cart_item, LineId, "Remove a cart line."),
        ("get_cart", get_cart, NoArgs, "Read the current cart. Call again after any change before quoting a total."),
        ("calculate_total", calculate_total, NoArgs, "Server-calculated total for the current cart."),
        ("create_order", create_order, NoArgs, "Lock the cart and quote it for the customer to confirm."),
        ("resolve_address", resolve_address, AddressInput,
         "Look up the delivery address. Split what the customer said into house, street, block, area and landmark."),
        ("confirm_address", confirm_address, CandidateNumber,
         "Save the place the customer confirmed. Only after they said yes in a message after you read it back."),
        ("check_delivery_zone", check_delivery_zone, NoArgs,
         "Check the confirmed address against the delivery area. The server decides, you cannot override it."),
        ("check_delivery_area", check_delivery_area, Query,
         "Instantly check a known area name before asking for house or block details."),
        ("reopen_order", reopen_order, NoArgs,
         "Unlock the cart so the customer can change items after checkout started. The quote is voided."),
        ("choose_pickup", choose_pickup, NoArgs,
         "Switch an out-of-zone delivery order to pickup at the restaurant."),
        ("confirm_order", confirm_order, NoArgs,
         "Place the order. Only after the customer clearly says yes to the quoted total."),
        ("cancel_order", cancel_order, NoArgs, "Cancel the order when the customer says so."),
        ("get_order_status", get_order_status, NoArgs, "Current order number, state and total."),
        ("check_open", check_open, NoArgs, "Whether the restaurant is open right now. The server decides; you cannot override it."),
        ("handoff_to_staff", handoff_to_staff, Reason, "Pass the customer to a person when you cannot help."),
    ]
    return [StructuredTool.from_function(func=guarded(name, fn), name=name, description=desc, args_schema=schema)
            for name, fn, schema, desc in specs]
