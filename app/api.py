import json
import time
import uuid
from collections import defaultdict
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.config import Settings, get_settings
from app.db import make_session_factory
from app.conversation import ConversationEngine
from app.delivery import AddressFields, configure_delivery
from app.errors import DomainError, NotFound, Unavailable
from app.geo import GeoapifyClient
from app.llm import build_models
from app.logging_setup import configure_logging, logger
from app.menu import MenuItem, load_menu
from app.orders import COMPLETED_WINDOW_H, OrderService
from app.schedule import load_schedule
from app.states import KITCHEN_ACTIONS, KITCHEN_COLUMNS, OrderState
from app.vapi import register_vapi_routes


class AddItem(BaseModel):
    item_id: int
    quantity: int = 1
    selections: dict[int, list[int]] = Field(default_factory=dict)


class ChatMessage(BaseModel):
    message: str


class Quantity(BaseModel):
    quantity: int


class AddressBody(BaseModel):
    raw: str
    house: str = ""
    street: str = ""
    block: str = ""
    area: str = ""
    landmark: str = ""


class LocationBody(BaseModel):
    latitude: float
    longitude: float


class ConfirmAddress(BaseModel):
    number: int


class Transition(BaseModel):
    to: OrderState
    reason: str | None = Field(default=None, max_length=300)


def _item_json(item: MenuItem) -> dict:
    return {
        "id": item.id, "name": item.name, "description": item.description, "price": str(item.price),
        "available": item.available, "category": item.category, "subcategory": item.subcategory,
        "option_groups": [{
            "id": g.id, "name": g.name, "required": g.required, "multiple": g.multiple,
            "min": g.min_selections, "max": g.max_selections, "price_behavior": g.price_behavior.value,
            "depends_on_group": g.linked_option_id,
            "choices": [{"id": c.id, "name": c.name, "price": str(c.price), "depends_on_choice": c.linked_choice_id}
                        for c in g.choices],
        } for g in item.groups],
    }


def create_app(settings: Settings | None = None, models: list | None = None, geo=None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # A broken menu or unreachable database stops startup instead of failing a customer later.
        menu = load_menu(settings.menu_path)
        sessions = make_session_factory(settings.database_url)
        app.state.menu = menu
        app.state.sessions = sessions
        schedule = load_schedule(settings.opening_time, settings.last_order_time, settings.closing_time)
        app.state.orders = OrderService(sessions, menu, settings.delivery_fee, schedule)
        geo_client = geo if geo is not None else (
            GeoapifyClient(settings.geoapify_api_key.get_secret_value()) if settings.geoapify_api_key else None)
        if hasattr(geo_client, "warm_up"):
            geo_client.warm_up()
        # One place resolves the delivery origin and logs where it came from, so the API and the
        # terminal demo can never disagree about where the restaurant is.
        app.state.delivery = configure_delivery(settings, app.state.orders, geo_client)
        chat_models = models if models is not None else build_models(settings)
        app.state.chat = (ConversationEngine(app.state.orders, chat_models, app.state.delivery)
                          if chat_models else None)
        if app.state.delivery.origin is None or geo_client is None:
            logger.warning("delivery_not_configured", extra={"has_pin": app.state.delivery.origin is not None,
                                                             "has_geo_key": geo_client is not None})
        elif not settings.restaurant_pin_verified:
            logger.warning("restaurant_pin_unverified")
        if schedule is None:
            logger.warning("schedule_not_configured")
        app.state.settings = settings
        logger.info("startup", extra={"chat_models": [type(m).__name__ for m in chat_models]})
        register_vapi_routes(app, app.state.orders)
        yield

    app = FastAPI(title="Hukm Karo order engine", lifespan=lifespan)

    # The Next.js demo frontend runs on a different origin during development and calls
    # these endpoints directly. Local dev or hosting behind the same reverse proxy.
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

    def error_body(code: str, message: str, request_id: str | None) -> dict:
        return {"error": {"code": code, "message": message, "request_id": request_id}}

    def _rid(request: Request) -> str | None:
        # The auth/rate middlewares run before log_requests sets the id, so fall back to None.
        return getattr(request.state, "request_id", None)

    @app.middleware("http")
    async def log_requests(request: Request, call_next):
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
        request.state.request_id = request_id
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception as exc:
            # Full detail goes to the log only. The caller gets a generic message, never a stack trace.
            logger.error("unhandled_error", exc_info=exc, extra={"request_id": request_id, "path": request.url.path})
            response = JSONResponse(status_code=500, content=error_body(
                "internal_error", "something went wrong, try again", request_id))
        response.headers["X-Request-ID"] = request_id
        logger.info("request", extra={"request_id": request_id, "method": request.method, "path": request.url.path,
                                      "status": response.status_code,
                                      "ms": round((time.perf_counter() - started) * 1000)})
        return response

    # Public endpoints: accessible without API auth.
    _PUBLIC_PREFIXES = ("/health", "/schedule", "/menu", "/vapi/", "/openapi.json", "/docs", "/redoc")

    # ---- API auth middleware -----------------------------------------------
    @app.middleware("http")
    async def api_auth(request: Request, call_next):
        secret = request.app.state.settings.api_secret
        if secret is None:
            return await call_next(request)
        if any(request.url.path.startswith(p) for p in _PUBLIC_PREFIXES):
            return await call_next(request)
        header = request.headers.get("Authorization", "")
        if header != f"Bearer {secret.get_secret_value()}":
            return JSONResponse(status_code=401, content=error_body(
                "unauthorized", "include a valid Authorization header", _rid(request)))
        return await call_next(request)

    # ---- rate limiter ------------------------------------------------------
    _RATE_WINDOW: defaultdict[str, list[float]] = defaultdict(list)

    @app.middleware("http")
    async def rate_limit(request: Request, call_next):
        settings = request.app.state.settings
        if settings.rate_limit_per_minute <= 0 or request.method in ("GET", "HEAD", "OPTIONS"):
            return await call_next(request)
        ip = request.client.host if request.client else "unknown"
        now = time.perf_counter()
        window = _RATE_WINDOW[ip]
        # Expire old entries.
        cutoff = now - 60
        while window and window[0] < cutoff:
            window.pop(0)
        if len(window) >= settings.rate_limit_per_minute:
            return JSONResponse(status_code=429, content=error_body(
                "rate_limited", "too many requests, slow down", _rid(request)))
        window.append(now)
        return await call_next(request)

    @app.exception_handler(DomainError)
    async def domain_error(request: Request, exc: DomainError):
        logger.warning("domain_error", extra={"code": exc.code, "path": request.url.path})
        return JSONResponse(status_code=exc.status,
                            content=error_body(exc.code, exc.message, request.state.request_id))

    @app.exception_handler(RequestValidationError)
    async def bad_request(request: Request, exc: RequestValidationError):
        first = exc.errors()[0] if exc.errors() else {}
        where = ".".join(str(p) for p in first.get("loc", []) if p != "body")
        return JSONResponse(status_code=422, content=error_body(
            "invalid_request", f"{where}: {first.get('msg', 'invalid')}", request.state.request_id))

    @app.get("/health")
    def health(request: Request):
        with request.app.state.sessions() as s:
            s.execute(text("SELECT 1"))
        return {"status": "ok", "menu_items": len(request.app.state.menu.items)}

    @app.get("/schedule")
    def schedule_status(request: Request):
        schedule = request.app.state.orders.schedule
        if schedule is None:
            return {"configured": False, "status": "open", "message": "Hours are not configured; open all day."}
        status, message = schedule.status()
        return {"configured": True, "status": status, "message": message,
                "opening": schedule.opening.strftime("%H:%M"),
                "last_order": schedule.last_order.strftime("%H:%M"),
                "closing": schedule.closing.strftime("%H:%M")}

    @app.get("/menu/search")
    def search(request: Request, q: str, limit: int = 10):
        found = request.app.state.menu.search(q, min(max(limit, 1), 25))
        return [{"id": i.id, "name": i.name, "price": str(i.price), "category": i.category} for i in found]

    @app.get("/menu/items/{item_id}")
    def menu_item(request: Request, item_id: int):
        item = request.app.state.menu.get(item_id)
        if item is None:
            raise NotFound("unknown_item", f"item {item_id} is not on the menu")
        return _item_json(item)

    @app.post("/orders", status_code=201)
    def create_order(request: Request, response: Response, idempotency_key: str = Header(alias="Idempotency-Key")):
        order, created = request.app.state.orders.create(idempotency_key)
        if not created:
            response.status_code = 200
        return order

    @app.get("/orders/active")
    def active_orders(request: Request):
        """Every order that has not finished, including live conversations.

        This is the wide feed the voice demo polls to show the caller their own order. It is not the
        kitchen board: most of what it returns is a customer still deciding.
        Registered before /orders/{order_id} so the literal path is never treated as an id.
        """
        return request.app.state.orders.list_active()

    @app.get("/orders/board")
    def kitchen_board(request: Request):
        """The kitchen display's whole contract in one payload: its columns, its buttons, its orders.

        The frontend renders what it is handed, so no state reaches a column and no button offers a
        move the state machine refuses. Defined in app/states.py and asserted by tests/test_board.py.
        """
        return {
            "columns": [{"key": key, "title": title, "states": [s.value for s in states]}
                        for key, title, states in KITCHEN_COLUMNS],
            "actions": {state.value: [{"label": label, "to": target.value, "tone": tone}
                                      for label, target, tone in actions]
                        for state, actions in KITCHEN_ACTIONS.items()},
            "orders": request.app.state.orders.board(),
            # Sent so the screen can explain its own retention window instead of hard-coding a copy.
            "completed_window_h": COMPLETED_WINDOW_H,
        }

    @app.get("/orders/{order_id}")
    def get_order(request: Request, order_id: int):
        return request.app.state.orders.get(order_id)

    @app.get("/orders/{order_id}/history")
    def order_history(request: Request, order_id: int):
        return request.app.state.orders.history(order_id)

    @app.post("/orders/{order_id}/chat")
    def chat(request: Request, order_id: int, body: ChatMessage):
        engine = request.app.state.chat
        if engine is None:
            raise Unavailable("chat_unavailable", "no language model is configured")
        return engine.reply(order_id, body.message)

    @app.post("/orders/{order_id}/chat/stream")
    async def chat_stream(request: Request, order_id: int, body: ChatMessage):
        """SSE reply: decouples time-to-first-sound from full LLM latency.

        The engine still returns one final reply (the LangGraph turn must settle before
        we speak), but we emit it in word-sized events over SSE; the browser feeds each
        event into TTS as it arrives. TTS then overlaps with the tail of the network
        transfer instead of waiting for the whole body.
        """
        engine = request.app.state.chat
        if engine is None:
            raise Unavailable("chat_unavailable", "no language model is configured")

        import asyncio
        from app.logging_setup import logger
        started = time.perf_counter()
        try:
            reply = engine.reply(order_id, body.message)
        except DomainError as exc:
            return JSONResponse(status_code=exc.status,
                                content=error_body(exc.code, exc.message, _rid(request)))

        ms = round((time.perf_counter() - started) * 1000)
        logger.info("stream_reply", extra={"order_id": order_id, "ms": ms, "chars": len(reply["reply"])})

        async def events():
            # SSE comments keep the connection warm and mark turn boundaries.
            yield "event: turn\n"
            yield f"data: {json.dumps({'ms': ms, 'order': reply['order']})}\n\n"
            text = reply["reply"]
            words = text.split()
            for i, word in enumerate(words):
                chunk = word + (" " if i < len(words) - 1 else "")
                yield f"event: word\ndata: {json.dumps({'w': chunk})}\n\n"
            yield "event: done\ndata: {}\n\n"

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/orders/{order_id}/items")
    def add_item(request: Request, order_id: int, body: AddItem):
        return request.app.state.orders.add_item(order_id, body.item_id, body.quantity, body.selections)

    @app.patch("/orders/{order_id}/items/{line_id}")
    def set_quantity(request: Request, order_id: int, line_id: str, body: Quantity):
        return request.app.state.orders.set_quantity(order_id, line_id, body.quantity)

    @app.delete("/orders/{order_id}/items/{line_id}")
    def remove_item(request: Request, order_id: int, line_id: str):
        return request.app.state.orders.remove_item(order_id, line_id)

    @app.post("/orders/{order_id}/address/resolve")
    def resolve_address(request: Request, order_id: int, body: AddressBody):
        return request.app.state.delivery.resolve(order_id, AddressFields(**body.model_dump()))

    @app.post("/orders/{order_id}/address/location")
    def resolve_location(request: Request, order_id: int, body: LocationBody):
        return request.app.state.delivery.resolve_location(order_id, body.latitude, body.longitude)

    @app.post("/orders/{order_id}/address/confirm")
    def confirm_address(request: Request, order_id: int, body: ConfirmAddress):
        return request.app.state.orders.confirm_address(order_id, body.number)

    @app.post("/orders/{order_id}/delivery/check")
    def check_delivery(request: Request, order_id: int):
        result = request.app.state.delivery.check(order_id)
        return {**result, "order": request.app.state.orders.get(order_id)}

    @app.post("/orders/{order_id}/transition")
    def transition(request: Request, order_id: int, body: Transition):
        return request.app.state.orders.transition(order_id, body.to, body.reason)

    return app


def get_app() -> FastAPI:
    """Entry point for `uvicorn app.api:get_app --factory`, so importing this module stays side-effect free."""
    return create_app()
