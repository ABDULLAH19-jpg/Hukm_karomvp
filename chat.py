"""Order by text in the terminal: python chat.py"""
import os
import time

from app.config import get_settings
from app.conversation import ConversationEngine
from app.db import make_session_factory
from app.delivery import configure_delivery
from app.geo import GeoapifyClient
from app.llm import LocalDemoModel, build_models
from app.logging_setup import logger
from app.menu import load_menu
from app.orders import OrderService


def main() -> None:
    settings = get_settings()
    logger.setLevel("WARNING")  # keep the terminal readable; the API logs everything
    models = build_models(settings)
    if os.getenv("HUKM_OFFLINE_DEMO", "false").lower() == "true":
        models = [LocalDemoModel()]
    elif not models:
        raise SystemExit("No usable Groq or Gemini provider is configured. Set a provider key or HUKM_OFFLINE_DEMO=true.")
    # The terminal MVP should start on a fresh machine even when local Postgres is not running.
    # Production/API deployments still use the configured database URL.
    try:
        sessions = make_session_factory(settings.database_url)
    except Exception as exc:
        logger.warning("database_unavailable_using_sqlite", extra={"error": type(exc).__name__})
        sessions = make_session_factory("sqlite:///./hukm_karo_chat.db")
    service = OrderService(sessions, load_menu(settings.menu_path), settings.delivery_fee)
    geo = GeoapifyClient(settings.geoapify_api_key.get_secret_value()) if settings.geoapify_api_key else None
    if geo:
        geo.warm_up()
    # Same origin resolution the API uses, so the two channels cannot disagree about where we deliver from.
    delivery = configure_delivery(settings, service, geo)
    engine = ConversationEngine(service, models, delivery)
    order, _ = service.create(f"terminal-{int(time.time())}")
    print(f"Order {order['number']} started. Type 'quit' to stop.\n")
    while True:
        try:
            text = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if text.lower() in {"quit", "exit"}:
            break
        if not text:
            continue
        out = engine.reply(order["id"], text)
        print(f"bot> {out['reply']}")
        print(f"     [{out['order']['state']}  total={out['order']['total'] or out['order']['subtotal']}]\n")


if __name__ == "__main__":
    main()
