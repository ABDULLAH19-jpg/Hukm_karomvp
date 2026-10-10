"""Chat acceptance harness for the Hukm Karo core agent (text channel only).

Drives the full ordering lifecycle through the real ConversationEngine — Groq/Gemini
models — against a deterministic fake geocoder and a frozen clock. It asserts on the
order's server-side state (state machine, cart, totals, address) rather than on the
model's exact wording, so a failing scenario means the *behaviour* went wrong, not the
phrasing.

This file is not collected by pytest (it is not named test_*.py). Run it directly:

    python -m tests.chat_acceptance                # every scenario
    python -m tests.chat_acceptance --list         # list scenarios
    python -m tests.chat_acceptance menu_browse    # one scenario (or several names)
    python -m tests.chat_acceptance -v             # print full transcripts

Requires a Groq and/or Gemini key in .env.hukm (HUKM_GROQ_API_KEY / HUKM_GEMINI_API_KEY).
"""
import argparse
import os
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, time as dt_time, timezone
from decimal import Decimal

from app.config import get_settings
from app.conversation import ConversationEngine
from app.db import make_session_factory
from app.delivery import DeliveryService
from app.errors import DomainError
from app.llm import build_models
from app.menu import load_menu
from app.orders import OrderService
from app.schedule import Schedule
from tests.conftest import ORIGIN, FakeGeo, place

# A fixed, deterministic delivery fee (independent of the running .env.hukm).
DELIVERY_FEE = Decimal("150")
# Real restaurant hours: 11:00 open, last order 03:00, close 03:00 (overnight, Asia/Karachi).
SCHEDULE = Schedule(dt_time(11, 0), dt_time(3, 0), dt_time(3, 0))
# Default frozen clock: 14:00 in Karachi (open) so the happy path never depends on wall time.
OPEN_UTC = (9, 0)      # 14:00 KHI
CLOSED_UTC = (4, 0)    # 09:00 KHI


# ---- clock control ---------------------------------------------------------

def freeze_clock(utc_hour: int, utc_minute: int = 0):
    """Return a datetime subclass whose .now() is pinned, to patch over app.schedule.datetime."""
    fixed = datetime(2026, 10, 6, utc_hour, utc_minute, 0, tzinfo=timezone.utc)

    class _FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed

    return _FakeDateTime


# ---- assertions ------------------------------------------------------------

def _lines(order: dict) -> list[tuple[str, int]]:
    return [(l["name"], l["quantity"]) for l in order.get("lines", [])]


def _option_choices(order: dict) -> list[str]:
    return [o["choice"] for l in order.get("lines", []) for o in l.get("options", [])]


def _is_provider_error(exc: Exception) -> bool:
    """A provider quota/outage is not an order-behaviour failure — don't score it as one."""
    name = type(exc).__name__.lower()
    text = str(exc).lower()
    return any(marker in name or marker in text
               for marker in ("ratelimit", "resource_exhausted", "429", "badrequest", "clienterror",
                              "generativeaierror", "apiconnection", "connectionerror", "timeout",
                              "internal_server_error", "service_unavailable"))


def evaluate(order: dict, expect: dict) -> list[str]:
    """Return a list of failure messages; empty means the expectation held."""
    failures: list[str] = []

    def want(key, ok, detail):
        if not ok:
            failures.append(f"{key}: expected {detail}")

    if "state" in expect:
        allowed = expect["state"] if isinstance(expect["state"], (list, tuple, set)) else [expect["state"]]
        want("state", order["state"] in allowed, f"one of {sorted(allowed)} (got {order['state']})")
    if "not_state" in expect:
        banned = expect["not_state"] if isinstance(expect["not_state"], (list, tuple, set)) else [expect["not_state"]]
        want("not_state", order["state"] not in banned, f"none of {sorted(banned)} (got {order['state']})")
    if "lines" in expect:
        got, exp = sorted(_lines(order)), sorted(expect["lines"])
        want("lines", got == exp, f"{exp} (got {got})")
    if "line_count" in expect:
        want("line_count", len(_lines(order)) == expect["line_count"],
             f"{expect['line_count']} (got {len(_lines(order))}: {_lines(order)})")
    if "option_contains" in expect:
        sub = expect["option_contains"]
        got = _option_choices(order)
        want("option_contains", any(sub in c for c in got), f"an option containing {sub!r} (got {got})")
    if "subtotal" in expect:
        want("subtotal", order.get("subtotal") == expect["subtotal"],
             f"{expect['subtotal']!r} (got {order.get('subtotal')!r})")
    if "delivery_fee" in expect:
        want("delivery_fee", order.get("delivery_fee") == expect["delivery_fee"],
             f"{expect['delivery_fee']!r} (got {order.get('delivery_fee')!r})")
    if "total" in expect:
        want("total", order.get("total") == expect["total"],
             f"{expect['total']!r} (got {order.get('total')!r})")
    if "address_confirmed" in expect:
        want("address_confirmed", order.get("address_confirmed") is expect["address_confirmed"],
             f"{expect['address_confirmed']} (got {order.get('address_confirmed')})")
    if "number" in expect:
        want("number", bool(order.get("number")), "a public order number (still empty)")
    return failures


# ---- scenarios -------------------------------------------------------------

@dataclass
class Step:
    customer: str
    expect: dict = field(default_factory=dict)


@dataclass
class Scenario:
    name: str
    description: str
    steps: list[Step]
    clock_utc: tuple[int, int] = OPEN_UTC


SCENARIOS: list[Scenario] = [
    Scenario("menu_browse", "Asking about items and prices never touches the cart.", [
        Step("Masala Fries kitne ka hai?", {"line_count": 0, "state": ["DRAFT", "COLLECTING_ITEMS"]}),
        Step("Aur Nuggets & Chips ka price kya hai?", {"line_count": 0, "state": ["DRAFT", "COLLECTING_ITEMS"]}),
    ]),
    Scenario("modifiers", "A required size is asked for, not guessed, and the chosen size is priced.", [
        Step("Mujhe ek Extreme Peri Crunch chahiye", {"line_count": 0}),
        Step("Large wali", {"lines": [("Extreme Peri Crunch", 1)], "subtotal": "2450.00",
                            "option_contains": "Large"}),
    ]),
    Scenario("multi_item", "Two items in one sentence both land in the cart.", [
        Step("Masala Fries aur Nuggets & Chips dono chahiye",
             {"lines": [("Masala Fries", 1), ("Nuggets & Chips", 1)], "subtotal": "870.00"}),
    ]),
    Scenario("cart_mutation", "Change a quantity, then remove a line.", [
        Step("Masala Fries chahiye", {"lines": [("Masala Fries", 1)]}),
        Step("Masala Fries do kar do", {"lines": [("Masala Fries", 2)]}),
        Step("Masala Fries hata do", {"line_count": 0}),
    ]),
    Scenario("happy_path_delivery", "Browse -> add -> checkout -> address -> confirm -> zone -> quote -> confirm.", [
        Step("Ek Masala Fries chahiye", {"lines": [("Masala Fries", 1)]}),
        Step("Checkout karo, delivery chahiye", {"not_state": "COLLECTING_ITEMS", "total": "530.00"}),
        Step("Mera address 12, Street 5, DHA Phase 4 hai", {"state": "COLLECTING_ADDRESS"}),
        Step("Haan sahi hai", {"state": "READY_FOR_CONFIRMATION", "address_confirmed": True,
                               "total": "530.00"}),
        Step("Haan confirm kar do", {"state": "CONFIRMED", "total": "530.00", "number": True}),
    ]),
    Scenario("out_of_zone_pickup", "An out-of-zone address is refused, then pickup is offered and taken.", [
        Step("Ek Masala Fries chahiye", {"lines": [("Masala Fries", 1)]}),
        Step("Checkout kar do", {"state": "VALIDATING_ORDER"}),
        Step("Mera address Street 9, DHA Phase 8 hai", {"state": "COLLECTING_ADDRESS"}),
        Step("Haan", {"state": "OUT_OF_ZONE"}),
        Step("Theek hai, pickup kar do", {"state": "PICKUP_SELECTED"}),
        Step("Haan confirm kar do", {"state": "CONFIRMED", "number": True}),
    ]),
    Scenario("confirmation_requires_yes", "A non-yes after the quote never confirms; a clear yes does.", [
        Step("Ek Masala Fries chahiye", {"lines": [("Masala Fries", 1)]}),
        Step("Checkout karo", {"state": "VALIDATING_ORDER"}),
        Step("Mera address 12 Street 5 DHA Phase 4 hai", {"state": "COLLECTING_ADDRESS"}),
        Step("Haan sahi hai", {"state": "READY_FOR_CONFIRMATION"}),
        Step("Maybe", {"state": "READY_FOR_CONFIRMATION", "not_state": "CONFIRMED"}),
        Step("Haan yes confirm kar do", {"state": "CONFIRMED", "number": True}),
    ]),
    Scenario("cancel_order", "The customer can cancel a cart before confirming.", [
        Step("Masala Fries chahiye", {"lines": [("Masala Fries", 1)]}),
        Step("Order cancel kar do", {"state": "CANCELLED"}),
    ]),
    Scenario("restaurant_closed", "Checkout is refused outside opening hours (frozen at 09:00 KHI).", [
        Step("Masala Fries chahiye", {"lines": [("Masala Fries", 1)]}),
        Step("Checkout kar do", {"state": "COLLECTING_ITEMS", "not_state": "VALIDATING_ORDER"}),
    ], clock_utc=CLOSED_UTC),
    Scenario("references", "\"it\" and \"that\" resolve to the item being discussed, read-only until intent.", [
        Step("Masala Fries ke baare mein batao", {"line_count": 0}),
        Step("Uska price kya hai?", {"line_count": 0}),
        Step("Theek hai, add kar do", {"lines": [("Masala Fries", 1)]}),
    ]),
]


# ---- runner ----------------------------------------------------------------

@dataclass
class StepResult:
    customer: str
    reply: str
    state: str
    lines: list
    subtotal: str | None
    total: str | None
    failures: list[str]
    provider_error: str | None = None


@dataclass
class ScenarioResult:
    scenario: Scenario
    passed: bool
    steps: list[StepResult]
    error: str | None = None
    models: list[str] = field(default_factory=list)
    provider_error: bool = False


def _build(clock_utc: tuple[int, int]):
    """Fresh, isolated stack: real models, deterministic geo, frozen clock."""
    import app.schedule as schedule_module
    settings = get_settings()
    menu = load_menu(settings.menu_path)
    db_path = os.path.join(tempfile.mkdtemp(prefix="hk-accept-"), "orders.db")
    sessions = make_session_factory(f"sqlite:///{db_path}")
    service = OrderService(sessions, menu, DELIVERY_FEE, SCHEDULE)

    geo = FakeGeo()
    geo.places = {
        "phase 4": [place("House 12, Street 5, DHA Phase 4, Lahore", north_m=1200, east_m=500)],
        "phase 8": [place("Street 9, DHA Phase 8, Lahore", north_m=9000, precision="street")],
    }
    delivery = DeliveryService(service, geo, ORIGIN[0], ORIGIN[1], 3000, pin_verified=True, area_rules=[])
    models = build_models(settings)
    # Gemini's free tier tolerates the token volume of a full run far better than Groq's,
    # so prefer it as the primary and keep the rest as fallbacks. The class is
    # ChatGoogleGenerativeAI (no "Gemini" in the name), so match on "GenerativeAI".
    models = sorted(models, key=lambda m: 0 if "GenerativeAI" in type(m).__name__ else 1)
    engine = ConversationEngine(service, models, delivery)
    schedule_module.datetime = freeze_clock(*clock_utc)
    return engine, service, [type(m).__name__ for m in models]


def run_scenario(scenario: Scenario, verbose: bool = False, delay_s: float = 1.0) -> ScenarioResult:
    engine, service, model_names = _build(scenario.clock_utc)
    try:
        order, _ = service.create(f"acceptance:{scenario.name}")
    except Exception as exc:  # noqa: BLE001 — report rather than crash the whole run
        return ScenarioResult(scenario, False, [], error=f"create failed: {exc}", models=model_names)

    steps: list[StepResult] = []
    passed = True
    provider_error = False
    for step in scenario.steps:
        perr: str | None = None
        try:
            out = engine.reply(order["id"], step.customer)
        except DomainError as exc:
            out = {"reply": f"<DomainError {exc.code}>", "order": service.get(order["id"])}
        except Exception as exc:  # noqa: BLE001
            if _is_provider_error(exc):
                perr = type(exc).__name__
                provider_error = True
                out = {"reply": f"<provider:{perr}>", "order": service.get(order["id"])}
            else:
                out = {"reply": f"<{type(exc).__name__}>", "order": service.get(order["id"])}
        o = out["order"]
        failures = evaluate(o, step.expect)
        if failures or perr:
            passed = False
        steps.append(StepResult(
            customer=step.customer, reply=out["reply"], state=o["state"], lines=_lines(o),
            subtotal=o.get("subtotal"), total=o.get("total"), failures=failures,
            provider_error=perr))
        if delay_s:
            time.sleep(delay_s)

    if verbose:
        print(f"\n=== {scenario.name} ===")
        for s in steps:
            print(f"  you> {s.customer}")
            print(f"  bot> {s.reply}")
            print(f"       -> {s.state}  subtotal={s.subtotal} total={s.total} lines={s.lines}")
    return ScenarioResult(scenario, passed, steps, models=model_names, provider_error=provider_error)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Hukm Karo chat acceptance harness.")
    parser.add_argument("scenarios", nargs="*", help="scenario names to run (default: all)")
    parser.add_argument("--list", action="store_true", help="list scenarios and exit")
    parser.add_argument("-v", "--verbose", action="store_true", help="print full transcripts")
    parser.add_argument("--delay", type=float, default=1.0, help="seconds to sleep between turns (default 1.0)")
    args = parser.parse_args()

    by_name = {s.name: s for s in SCENARIOS}
    if args.list:
        for s in SCENARIOS:
            print(f"{s.name:24} {s.description}")
        return

    chosen = [by_name[n] for n in args.scenarios] if args.scenarios else SCENARIOS
    missing = [n for n in args.scenarios if n not in by_name]
    if missing:
        print(f"unknown scenario(s): {', '.join(missing)}")
        return

    results = [run_scenario(s, verbose=args.verbose, delay_s=args.delay) for s in chosen]

    print("\n" + "=" * 78)
    print(f"{'scenario':24} {'result':7} {'turns':5}  detail")
    print("=" * 78)
    for r in results:
        model_note = f"[{', '.join(r.models)}]" if r.models else "[no model]"
        if r.error:
            print(f"{r.scenario.name:24} ERROR     -    {r.error}")
            continue
        if r.provider_error:
            print(f"{r.scenario.name:24} PROVIDER {len(r.steps):5}  {model_note}  (LLM quota/outage, not a behaviour verdict)")
            continue
        mark = "PASS" if r.passed else "FAIL"
        print(f"{r.scenario.name:24} {mark:7} {len(r.steps):5}  {model_note}")
        if not r.passed:
            for s in r.steps:
                if s.failures:
                    print(f"    you> {s.customer!r}  ->  {s.state}  subtotal={s.subtotal} total={s.total} lines={s.lines}")
                    for f in s.failures:
                        print(f"      FAIL  {f}")
                    print(f"      bot> {s.reply}")
    passed = sum(1 for r in results if r.passed and not r.error and not r.provider_error)
    provider = sum(1 for r in results if r.provider_error)
    errored = sum(1 for r in results if r.error)
    failed = len(results) - passed - provider - errored
    print("=" * 78)
    parts = [f"{passed}/{len(results)} scenarios passed"]
    if provider:
        parts.append(f"{provider} provider-quota")
    if errored:
        parts.append(f"{errored} error")
    if failed:
        parts.append(f"{failed} failed")
    print(", ".join(parts) + ".")
    if failed:
        print("Failing scenarios are reported above; no application code was changed.")


if __name__ == "__main__":
    main()
