"""Vapi voice-channel webhook: receives tool-call events, runs the deterministic tools, returns results.

The Vapi assistant mirrors our SYSTEM_PROMPT but runs its own STT/TTS. Our server exposes exactly the
same tool set so the LLM-to-tool path stays identical across text and voice.
"""
import json
import time

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import delete, select

from app.db import CallTranscriptRow
from app.errors import DomainError
from app.logging_setup import logger
from app.orders import OrderService
from app.tools import build_tools

# ---- Vapi webhook models ---------------------------------------------------


class VapiToolCall(BaseModel):
    id: str = ""
    name: str = ""
    parameters: dict = Field(default_factory=dict)


class VapiToolWithCall(BaseModel):
    name: str = ""
    toolCall: VapiToolCall = Field(default_factory=VapiToolCall)


class VapiCallRef(BaseModel):
    id: str = ""


class VapiMessage(BaseModel):
    type: str = ""          # "tool-calls", "transcript", "end-of-call-report", etc.
    call: VapiCallRef = Field(default_factory=VapiCallRef)
    toolWithToolCallList: list[VapiToolWithCall] = Field(default_factory=list)
    toolCallList: list[VapiToolCall] = Field(default_factory=list)
    role: str = ""          # "user" | "assistant" (transcript events)
    transcript: str = ""    # transcript text (transcript events)
    transcriptType: str = ""  # "partial" | "final"


class VapiEnvelope(BaseModel):
    message: VapiMessage = Field(default_factory=VapiMessage)


# ---- transcript store -------------------------------------------------------

class CallTranscripts:
    """The caller's own words, as the `heard` evidence guard reads them.

    In the database, not in memory: a restart during a call used to erase the words that prove the
    customer asked for what the model is about to add. The window is short because the guards only
    look at the last few turns.
    """

    WINDOW = 8

    def __init__(self, sessions) -> None:
        self.sessions = sessions

    def record(self, call_id: str, role: str, text: str) -> None:
        said = text.strip()
        if role != "user" or not said or not call_id:
            return
        with self.sessions.begin() as s:
            s.add(CallTranscriptRow(call_id=call_id, role=role, text=said[:2000]))

    def heard(self, call_id: str) -> list[str]:
        with self.sessions() as s:
            rows = s.scalars(select(CallTranscriptRow).where(CallTranscriptRow.call_id == call_id)
                             .order_by(CallTranscriptRow.id.desc()).limit(self.WINDOW)).all()
        return [r.text for r in reversed(rows)]

    def forget(self, call_id: str) -> None:
        """The call ended, so the customer's speech is no longer needed for anything."""
        with self.sessions.begin() as s:
            s.execute(delete(CallTranscriptRow).where(CallTranscriptRow.call_id == call_id))


# ---- tool-definition export ------------------------------------------------


def _deref_schema(schema: dict, defs: dict) -> dict:
    """Inline all $ref entries in a JSON Schema subtree so Vapi's validator accepts it.

    Vapi requires every 'items' and property to have a concrete type field, not a $ref.
    """
    if not isinstance(schema, dict):
        return schema
    if "$ref" in schema:
        ref_path = schema["$ref"]
        # refs look like "#/$defs/Selection"
        parts = ref_path.split("/")
        key = parts[-1] if parts else ""
        if key in defs:
            return _deref_schema(defs[key], defs)
        return schema
    out = {}
    for k, v in schema.items():
        if k == "$defs":
            continue
        if isinstance(v, dict):
            out[k] = _deref_schema(v, defs)
        elif isinstance(v, list):
            out[k] = [_deref_schema(item, defs) for item in v]
        else:
            out[k] = v
    return out


def vapi_tool_definitions() -> list[dict]:
    """Export every tool as a Vapi-compatible (OpenAI function-calling) definition."""
    from app.tools import (AddToCart, AddressInput, CandidateNumber, ItemId, LineId, NoArgs,
                            Query, Reason, UpdateCartItem)

    TOOL_SCHEMAS: list[tuple[str, type[BaseModel], str]] = [
        ("search_menu", Query, "Find menu items by name or category. Only items returned here exist."),
        ("get_menu_item", ItemId,
         "Get one item's option groups, which are required, and their choices. Call before add_to_cart for items with options."),
        ("check_availability", ItemId, "Check whether an item can be ordered right now."),
        ("add_to_cart", AddToCart,
         "Add an item with the customer's option choices. Prices are set by the server, never by you."),
        ("update_cart_item", UpdateCartItem, "Change the quantity of an existing cart line."),
        ("remove_cart_item", LineId, "Remove a cart line."),
        ("get_cart", NoArgs, "Read the current cart. Call again after any change before quoting a total."),
        ("calculate_total", NoArgs, "Server-calculated total for the current cart."),
        ("create_order", NoArgs, "Lock the cart and quote it for the customer to confirm."),
        ("resolve_address", AddressInput,
         "Look up the delivery address. Split what the customer said into house, street, block, area and landmark."),
        ("confirm_address", CandidateNumber,
         "Save the place the customer confirmed. Only after they said yes in a message after you read it back."),
        ("check_delivery_zone", NoArgs,
         "Check the confirmed address against the delivery area. The server decides, you cannot override it."),
        ("check_delivery_area", Query,
         "Instantly check a known area name before asking for house or block details."),
        ("reopen_order", NoArgs,
         "Unlock the cart so the customer can change items after checkout started. The quote is voided."),
        ("choose_pickup", NoArgs,
         "Switch an out-of-zone delivery order to pickup at the restaurant."),
        ("confirm_order", NoArgs,
         "Place the order. Only after the customer clearly says yes to the quoted total."),
        ("cancel_order", NoArgs, "Cancel the order when the customer says so."),
        ("get_order_status", NoArgs, "Current order number, state and total."),
        ("check_open", NoArgs, "Whether the restaurant is open right now. The server decides; you cannot override it."),
        ("handoff_to_staff", Reason, "Pass the customer to a person when you cannot help."),
    ]
    out = []
    for name, model, description in TOOL_SCHEMAS:
        raw = model.model_json_schema()
        defs = raw.get("$defs", {})
        dereffed = _deref_schema(raw, defs)
        out.append({
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": {
                    "type": "object",
                    "properties": dereffed.get("properties", {}),
                    "required": dereffed.get("required", []),
                },
            },
        })
    return out


# ---- webhook handler -------------------------------------------------------


def _verify_auth(request: Request) -> None:
    settings = request.app.state.settings
    if settings.vapi_webhook_secret is None:
        # No secret configured — allow unauthenticated access for local dev.
        return
    header = request.headers.get("Authorization", "")
    expected = f"Bearer {settings.vapi_webhook_secret.get_secret_value()}"
    if header != expected:
        raise HTTPException(status_code=401, detail="unauthorized")


async def handle_vapi_webhook(service: OrderService, request: Request) -> dict:
    """Handle every Vapi server event: transcript storage, tool-call execution."""
    _verify_auth(request)
    body = await request.json()
    try:
        msg = VapiMessage.model_validate(body.get("message", {}))
    except Exception as exc:
        logger.warning("vapi_bad_message", extra={"error": str(exc)})
        return {}  # Vapi expects a 200 even for unrecognised events

    call_id = msg.call.id
    transcripts = CallTranscripts(request.app.state.sessions)

    # --- transcript events --------------------------------------------------
    if msg.type in ("transcript", "transcript-update"):
        transcripts.record(call_id, msg.role, msg.transcript)
        return {}  # no response needed

    # --- end-of-call: drop the caller's words -------------------------------
    if msg.type == "end-of-call-report":
        transcripts.forget(call_id)
        return {}

    # --- tool-calls ---------------------------------------------------------
    if msg.type != "tool-calls":
        return {}  # unrecognised event type, ignored

    if not call_id:
        logger.warning("vapi_no_call_id")
        return {"results": []}

    # Bind the call to its order through the idempotency key, on every batch. OrderService.create
    # returns the existing order for a repeated key, so a retry, a duplicated webhook, or a reconnect
    # after a restart all land on the same order rather than making a second one. There is no
    # in-memory map here to go stale, because there is nothing the database does not already know.
    started = time.perf_counter()
    order, _ = service.create(f"vapi:{call_id}")
    order_id = order["id"]

    # Build tools bound to this order, with the `heard` callback.
    delivery = getattr(request.app.state, "delivery", None)
    tools = build_tools(service, order_id, delivery=delivery, heard=lambda: transcripts.heard(call_id))
    by_name = {t.name: t for t in tools}

    # Execute each tool call.
    calls = msg.toolCallList or [t.toolCall for t in msg.toolWithToolCallList or []]
    results = []
    for tc in calls:
        if tc.name not in by_name:
            results.append({"name": tc.name, "toolCallId": tc.id,
                            "result": json.dumps({"ok": False, "error": "unknown_tool",
                                                  "message": f"no tool named {tc.name}"})})
            continue
        try:
            result = by_name[tc.name].invoke(tc.parameters or {})
            ms = round((time.perf_counter() - started) * 1000)
            logger.info("vapi_tool_call", extra={"call_id": call_id, "order_id": order_id,
                                                  "tool": tc.name, "ok": result.get("ok", True), "ms": ms})
        except DomainError as exc:
            result = {"ok": False, "error": exc.code, "message": exc.message}
        except Exception:
            logger.exception("vapi_tool_crash", extra={"call_id": call_id, "tool": tc.name})
            result = {"ok": False, "error": "internal_error", "message": "that did not work, try again"}
        results.append({"name": tc.name, "toolCallId": tc.id, "result": json.dumps(result)})
    return {"results": results}


# ---- FastAPI route factory -------------------------------------------------


def register_vapi_routes(app: FastAPI, service: OrderService) -> None:
    @app.post("/vapi/tool-calls")
    async def vapi_tool_calls(request: Request):
        return await handle_vapi_webhook(service, request)