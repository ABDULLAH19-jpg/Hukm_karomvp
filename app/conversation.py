"""LangGraph conversation engine: the model decides what to say, tools and the state machine decide what is allowed.

    START -> agent -> (tool calls?) -> tools -> agent ... -> settle -> END
"""
import json
import re
import time
from typing import Annotated, TypedDict

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.errors import GraphRecursionError
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from app.delivery import DeliveryService
from app.errors import DomainError
from app.logging_setup import logger
from app.orders import OrderService
from app.states import TERMINAL, OrderState
from app.tools import build_tools

MAX_MESSAGE_CHARS = 500
HISTORY_LIMIT = 20
OLD_TOOL_RESULT_CHARS = 240  # results from earlier turns are context only, the cart is re-read each turn
MAX_STEPS = 14  # graph steps per customer message, stops a model that loops on tools
READ_ONLY_TOOLS = {"search_menu", "get_menu_item", "check_availability", "get_cart", "calculate_total",
                   "get_order_status", "check_open", "check_delivery_area"}
MUTATING_TOOLS = {"add_to_cart", "update_cart_item", "remove_cart_item", "create_order", "confirm_order",
                  "resolve_address", "confirm_address", "check_delivery_zone", "choose_pickup",
                  "reopen_order", "cancel_order", "handoff_to_staff"}


def _is_menu_information_query(text: str) -> bool:
    """Keep a question about an item from entering the cart flow."""
    value = re.sub(r"[^a-z0-9? ]+", " ", text.lower()).strip()
    if re.search(r"\b(add|order|take|get me|i ll have|i will have|want)\b", value):
        return False
    return bool("?" in text or re.search(r"\b(what|tell me about|how much|what comes|ingredients|contains|describe)\b", value))

SYSTEM_PROMPT = """You take food orders for Hukm Karo, a restaurant in Lahore, over chat or phone.

Language: reply in the language the customer uses. They may write English, Roman Urdu (Urdu in English letters), or a mix. Match them. Always write in the Latin alphabet, never in Urdu or Hindi script. Keep every reply to one or two short sentences, because this also runs over voice.

Rules you cannot break:
- Only offer items that search_menu returns. Never invent an item, option or price. Never state a price or total that did not come from a tool result.
- Treat menu questions as read-only: describe the last discussed item when the customer says “it” or “that”, and do not add it, ask for modifiers, or start checkout unless the customer clearly expresses ordering intent.
- Before adding an item that has options, call get_menu_item. If a required option is missing, ask the customer for it. Never pick one for them, not even a cheapest or default one.
- In add_to_cart, fill customer_words for every option with the customer's exact words that chose it. If you cannot quote such words, the customer has not chosen yet: ask, do not add.
- Name options only exactly as get_menu_item lists them. If you have not called get_menu_item for that item this turn, call it before you describe its options.
- Some options depend on another choice (a topping price depends on the size). Ask for the parent choice first.
- Resolve references, repetitions, corrections, spelling variants, and relative choices from the full conversation context. Ask only for information that is genuinely missing, and send the canonical option names returned by get_menu_item.
- For an item with repeated required groups (for example, two burger groups), every required group must be included in one add_to_cart call. Match choices by their canonical names even when the menu uses different choice IDs in each group.
- After any change to the cart, call get_cart and quote what it returns.
- If a tool returns ok false, tell the customer plainly and ask what to do. Do not retry the same call.
- Checkout order: create_order, then resolve_address, then confirm_address, then check_delivery_zone, then read the quote and total back and ask the customer to confirm.
- If the customer only asks whether delivery is available in an area, call check_delivery_area first. Do not ask for a house or block number when it returns within or outside. Only ask for a full address when it returns need_address or when placing a delivery order.
- Addresses: split what the customer said into house, street, block, area and landmark for resolve_address. Never invent a field. If the result says need_detail or not_found, ask for a block, street, house number or landmark. After two failed tries, hand off to staff.
- When resolve_address finds places, read the place back and wait for the customer to answer in their next message. Only then call confirm_address with the number they chose. Never choose a place for them.
- If several places were offered, the customer must say which one by number. A plain "yes" does not choose, so ask again.
- The server decides the delivery area, which is 3 km by road. Pass on tell_customer from check_delivery_zone. If it fails to run, try once more, then hand off. If a place looks_outside_zone, mention gently that it may be too far.
- Call confirm_order only after the customer clearly says yes to that quoted total, in a message that arrives after you asked. If they change anything, quote again.
- When the customer asks to check out, run the checkout tools in that same turn. If an item still has an unanswered required option, say it is not in the cart yet and ask once.
- To change items after checkout started, call reopen_order first, then edit, then check out again.
- If the customer wants a person, or you are stuck, call handoff_to_staff.

Understanding the customer:
- "nahi aik hi kar do" means set the quantity to 1. "coke nahi sprite" means remove the coke and add a sprite. "large wali" refers to the Large size of the item being discussed. "repeat karna" or "wapas bolo" means read the cart again.
- If a request could match several items, ask which one. Do not guess.
"""

CLOSED_MESSAGE = "This order is already {state}. Please start a new order."
FALLBACK_REPLY = "Sorry, I am having trouble right now. Please try again in a moment."


class ChatState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    address_armed: bool  # a place was read back on an earlier turn and the customer has now replied
    confirm_armed: bool  # True only if the quote was shown on an earlier turn and nothing changed since


def _text(message: BaseMessage) -> str:
    content = message.content
    if isinstance(content, list):  # Gemini returns content blocks
        content = " ".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in content)
    return str(content).strip()


def _tidy(reply: str) -> str:
    """Drop sentences a model repeated verbatim, a known glitch when a provider is under load."""
    seen, kept = set(), []
    for sentence in re.split(r"(?<=[.?!])\s*", reply):
        key = sentence.strip().lower()
        if key and key not in seen:
            seen.add(key)
            kept.append(sentence.strip())
    return " ".join(kept)


def _recent(messages: list[BaseMessage]) -> list[BaseMessage]:
    window = messages[-HISTORY_LIMIT:]
    # Never start on a tool result whose tool call was cut off.
    start = next((i for i, m in enumerate(window) if isinstance(m, HumanMessage)), 0)
    window = window[start:]
    last_human = max((i for i, m in enumerate(window) if isinstance(m, HumanMessage)), default=0)
    # Big tool results (a pizza's full option list) from earlier turns cost tokens and are stale anyway.
    return [m.model_copy(update={"content": m.content[:OLD_TOOL_RESULT_CHARS]})
            if isinstance(m, ToolMessage) and i < last_human and len(m.content) > OLD_TOOL_RESULT_CHARS else m
            for i, m in enumerate(window)]


# Tools shown to the model per order state. Fewer schemas means fewer tokens, and confirm_order is not
# even offered until the quote is on the table.
_ALWAYS = ["get_cart", "check_open", "handoff_to_staff", "cancel_order"]
TOOLS_BY_STATE = {
    "DRAFT": ["search_menu", "get_menu_item", "check_availability", "check_delivery_area", "add_to_cart", "update_cart_item",
              "remove_cart_item", "create_order", *_ALWAYS],
    "VALIDATING_ORDER": ["resolve_address", "reopen_order", *_ALWAYS],
    "COLLECTING_ADDRESS": ["resolve_address", "confirm_address", "check_delivery_zone", "reopen_order", *_ALWAYS],
    "CHECKING_DELIVERY": ["check_delivery_zone", "resolve_address", "reopen_order", *_ALWAYS],
    "READY_FOR_CONFIRMATION": ["confirm_order", "resolve_address", "reopen_order", *_ALWAYS],
}
TOOLS_BY_STATE["OUT_OF_ZONE"] = ["resolve_address", "choose_pickup", *_ALWAYS]
TOOLS_BY_STATE["PICKUP_SELECTED"] = ["get_cart", "confirm_order", "reopen_order", *_ALWAYS]
TOOLS_BY_STATE["COLLECTING_ITEMS"] = TOOLS_BY_STATE["DRAFT"]
OTHER_STATES_TOOLS = ["get_order_status", "handoff_to_staff"]

NEXT_STEP = {
    "DRAFT": "Greet and take the order.",
    "COLLECTING_ITEMS": "Keep taking items. When the customer is done or asks to check out, call create_order now, "
                        "before talking about anything else.",
    "VALIDATING_ORDER": "Checkout has started. Ask for the delivery address, then call resolve_address with it "
                        "split into fields.",
    "COLLECTING_ADDRESS": "If resolve_address found places, wait for the customer's answer, then confirm_address. "
                          "Once an address is confirmed, call check_delivery_zone. If more detail is needed, ask once.",
    "OUT_OF_ZONE": "Offer exactly four choices: pickup, change the delivery address, talk to staff, or cancel. "
                   "Call choose_pickup, resolve_address, handoff_to_staff, or cancel_order after the customer chooses.",
    "CHECKING_DELIVERY": "The map check did not finish. Call check_delivery_zone once more, else hand off to staff.",
    "READY_FOR_CONFIRMATION": "Read the items and total back and ask for a clear yes. Call confirm_order only after "
                              "a yes that arrives in a later message.",
    "CONFIRMED": "The order is placed. Thank the customer.",
}


def _context(order: dict) -> str:
    lines = "; ".join(f"{l['quantity']} x {l['name']}"
                      + (f" ({', '.join(o['choice'] for o in l['options'])})" if l["options"] else "")
                      + f" = {l['line_total']} [{l['line_id']}]" for l in order["lines"]) or "empty"
    return (f"Order {order['number']} is {order['state']}. Cart: {lines}. Subtotal {order['subtotal']}, "
            f"total {order['total']}. Next: {NEXT_STEP.get(order['state'], 'Help the customer.')}")


class ConversationEngine:
    def __init__(self, service: OrderService, models: list[BaseChatModel], delivery: DeliveryService | None = None):
        if not models:
            raise ValueError("at least one chat model is required")
        self.service = service
        self.models = models
        self.delivery = delivery
        self.memory = MemorySaver()  # chat history only; the order itself lives in the database

    def reply(self, order_id: int, text: str) -> dict:
        text = text.strip()
        if not text:
            raise DomainError("empty_message", "say something to continue")
        if len(text) > MAX_MESSAGE_CHARS:
            raise DomainError("message_too_long", f"keep messages under {MAX_MESSAGE_CHARS} characters")
        order = self.service.get(order_id)  # also raises NotFound
        if OrderState(order["state"]) in TERMINAL:
            return {"reply": CLOSED_MESSAGE.format(state=order["state"]), "order": order}

        graph = self._graph(order_id)
        config = {"configurable": {"thread_id": str(order_id)}, "recursion_limit": MAX_STEPS}
        try:
            result = graph.invoke({"messages": [HumanMessage(text)],
                                   "address_armed": self.service.address_pending(order_id)}, config)
            reply = _tidy(_text(next(m for m in reversed(result["messages"]) if isinstance(m, AIMessage))))
        except GraphRecursionError:
            logger.error("conversation_loop", extra={"order_id": order_id})
            reply = FALLBACK_REPLY
        except Exception:
            logger.exception("conversation_failed", extra={"order_id": order_id})
            reply = FALLBACK_REPLY
        return {"reply": reply or FALLBACK_REPLY, "order": self.service.get(order_id)}

    # ---- graph -------------------------------------------------------------

    def _graph(self, order_id: int):
        heard_box: dict[str, list[str]] = {"texts": []}
        tools = build_tools(self.service, order_id, delivery=self.delivery, heard=lambda: heard_box["texts"])
        by_name = {t.name: t for t in tools}
        service = self.service

        def llm_for(state: str):
            subset = [by_name[n] for n in TOOLS_BY_STATE.get(state, OTHER_STATES_TOOLS)]
            bound = [m.bind_tools(subset) for m in self.models]
            return bound[0].with_fallbacks(bound[1:]) if len(bound) > 1 else bound[0]

        def agent(state: ChatState):
            order = service.get(order_id)
            started = time.perf_counter()
            reply = llm_for(order["state"]).invoke(
                [SystemMessage(SYSTEM_PROMPT + "\n" + _context(order)), *_recent(state["messages"])])
            logger.info("llm_call", extra={
                "order_id": order_id, "state": order["state"], "ms": round((time.perf_counter() - started) * 1000),
                "model": reply.response_metadata.get("model_name"), "tool_calls": [c["name"] for c in reply.tool_calls]})
            return {"messages": [reply]}

        def run_tools(state: ChatState):
            armed = state.get("confirm_armed", False)
            addr_armed = state.get("address_armed", False)
            heard_box["texts"] = [_text(m) for m in state["messages"] if isinstance(m, HumanMessage)][-4:]
            current_text = heard_box["texts"][-1] if heard_box["texts"] else ""
            info_turn = _is_menu_information_query(current_text)
            out: list[ToolMessage] = []
            for call in state["messages"][-1].tool_calls:
                name = call["name"]
                if info_turn and name in MUTATING_TOOLS:
                    result = {"ok": False, "error": "read_only_menu_question",
                              "message": "This is a menu information question. Answer it using the menu tools; do not change the cart or start checkout."}
                elif name == "confirm_order" and not (
                        armed and service.get(order_id)["state"] == OrderState.READY_FOR_CONFIRMATION.value):
                    logger.warning("confirm_blocked", extra={"order_id": order_id})
                    result = {"ok": False, "error": "confirmation_needed",
                              "message": "Read the quote and total to the customer and wait for their yes "
                                         "in their next message before confirming."}
                elif name == "confirm_address" and not addr_armed:
                    logger.warning("address_confirm_blocked", extra={"order_id": order_id})
                    result = {"ok": False, "error": "confirmation_needed",
                              "message": "Read the place back and wait for the customer's yes in their next message."}
                elif name not in by_name:
                    result = {"ok": False, "error": "unknown_tool", "message": f"no tool named {name}"}
                else:
                    result = by_name[name].invoke(call["args"])
                    if name not in READ_ONLY_TOOLS:
                        armed = False  # anything that can change the order voids an earlier quote
                        if name != "confirm_address":
                            addr_armed = False  # a fresh lookup needs a fresh customer answer
                out.append(ToolMessage(json.dumps(result, ensure_ascii=False), tool_call_id=call["id"], name=name))
            return {"messages": out, "confirm_armed": armed, "address_armed": addr_armed}

        def settle(state: ChatState):
            # The turn ends here. If the quote is on the table now, the customer's next message may confirm it.
            return {"confirm_armed": service.get(order_id)["state"] == OrderState.READY_FOR_CONFIRMATION.value}

        def route(state: ChatState):
            return "tools" if getattr(state["messages"][-1], "tool_calls", None) else "settle"

        g = StateGraph(ChatState)
        g.add_node("agent", agent)
        g.add_node("tools", run_tools)
        g.add_node("settle", settle)
        g.add_edge(START, "agent")
        g.add_conditional_edges("agent", route, {"tools": "tools", "settle": "settle"})
        g.add_edge("tools", "agent")
        g.add_edge("settle", END)
        return g.compile(checkpointer=self.memory)
