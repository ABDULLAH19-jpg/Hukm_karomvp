"""Real-model smoke test, not collected by pytest. Run: python -m tests.live_conversation"""
import os
import tempfile
import time

from app.config import get_settings
from app.conversation import ConversationEngine
from app.db import make_session_factory
from app.llm import build_models
from app.logging_setup import configure_logging
from app.menu import load_menu
from app.orders import OrderService

TURNS = [
    "salam, aik Extreme Peri Crunch chahiye",
    "large wali",
    "aur 2 coke bhi",
    "nahi aik hi kar do coke",
    "500 ml wali",
    "repeat karna order",
]

configure_logging()
settings = get_settings()
tmp = os.path.join(tempfile.mkdtemp(), "live.db")
service = OrderService(make_session_factory(f"sqlite:///{tmp}"), load_menu(settings.menu_path))
engine = ConversationEngine(service, build_models(settings))
order, _ = service.create("live")
for turn in TURNS:
    t = time.perf_counter()
    out = engine.reply(order["id"], turn)
    o = out["order"]
    print(f"\nCUSTOMER: {turn}\nBOT ({time.perf_counter()-t:.1f}s): {out['reply']}\n  -> {o['state']} subtotal={o['subtotal']} total={o['total']}")
print("\nHISTORY:", [e["to"] for e in service.history(order["id"])])
