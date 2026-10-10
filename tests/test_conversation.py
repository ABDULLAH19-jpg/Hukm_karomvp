import itertools

import pytest
from fastapi.testclient import TestClient
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from app.api import create_app
from app.conversation import FALLBACK_REPLY, ConversationEngine
from app.db import make_session_factory
from app.errors import DomainError
from app.orders import OrderService
from tests.conftest import PIZZA, item_named

_ids = itertools.count(1)


class ScriptedModel(BaseChatModel):
    """Plays back prepared AI messages, one per model call. An Exception entry is raised instead."""
    script: list
    calls: int = 0
    offered: list = []  # tool names offered on each model call

    def bind_tools(self, tools, **kwargs):
        self.offered.append({t.name for t in tools})
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        if self.calls >= len(self.script):
            raise RuntimeError("script exhausted")
        step = self.script[self.calls]
        self.calls += 1
        if isinstance(step, Exception):
            raise step
        return ChatResult(generations=[ChatGeneration(message=step)])

    @property
    def _llm_type(self):
        return "scripted"


def call(name, **args):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": f"c{next(_ids)}"}])


def say(text):
    return AIMessage(content=text)


@pytest.fixture
def service(menu, db_url):
    return OrderService(make_session_factory(db_url), menu)


@pytest.fixture
def order_id(service):
    order, _ = service.create("conv-test")
    sel = {PIZZA["crust"]: [PIZZA["deep_pan"]], PIZZA["size"]: [PIZZA["regular"]],
           PIZZA["topping"]: [PIZZA["cheese_regular"]]}
    service.add_item(order["id"], item_named(service.menu, "Chicken Supreme").id, 1, sel)
    return order["id"]


def engine_for(service, *scripts, delivery=None):
    return ConversationEngine(service, [ScriptedModel(script=s) for s in scripts], delivery)


def lookup():
    # Fresh messages on every call: LangGraph identifies messages by id, so reusing one object twice in a
    # script would overwrite the earlier message instead of appending.
    return [call("create_order"), call("resolve_address", raw="12 street 5 phase 4", house="12", street="5",
                                       area="DHA Phase 4")]


def quote():
    return [call("confirm_address", number=1), call("check_delivery_zone")]


@pytest.fixture
def at_quote(service, order_id, delivery_for):
    """An engine whose customer has confirmed the address and been quoted: state READY_FOR_CONFIRMATION."""
    def build(third_turn):
        engine = engine_for(service, [*lookup(), say("Is it House 12, Street 5, DHA Phase 4?"),
                                      *quote(), say("Total 1580. Confirm?"), *third_turn],
                            delivery=delivery_for(service))
        engine.reply(order_id, "checkout, 12 street 5 phase 4")
        engine.reply(order_id, "haan")
        assert service.get(order_id)["state"] == "READY_FOR_CONFIRMATION"
        return engine
    return build


def test_model_cannot_save_an_address_in_the_turn_it_looks_it_up(service, order_id, delivery_for):
    engine = engine_for(service, [*lookup(), call("confirm_address", number=1), say("Is this right?")],
                        delivery=delivery_for(service))
    out = engine.reply(order_id, "checkout, 12 street 5 phase 4")
    assert out["order"]["state"] == "COLLECTING_ADDRESS" and out["order"]["address_confirmed"] is False


def test_customer_reply_then_confirms_the_address(service, order_id, delivery_for):
    engine = engine_for(service, [*lookup(), say("Is this right?"), call("confirm_address", number=1), say("Saved.")],
                        delivery=delivery_for(service))
    engine.reply(order_id, "checkout, 12 street 5 phase 4")
    assert engine.reply(order_id, "haan")["order"]["address_confirmed"] is True


def test_a_fresh_lookup_voids_an_earlier_pending_address(service, order_id, delivery_for):
    engine = engine_for(service, [*lookup(), say("Is it House 12?"),
                                  call("resolve_address", raw="phase 5 masjid", landmark="masjid"),
                                  call("confirm_address", number=1), say("Which masjid?")],
                        delivery=delivery_for(service))
    engine.reply(order_id, "checkout, 12 street 5 phase 4")
    out = engine.reply(order_id, "nahi, phase 5 masjid ke paas")
    assert out["order"]["address_confirmed"] is False  # the new candidates were never answered


def test_model_cannot_confirm_in_the_same_turn_it_quotes(service, order_id, delivery_for):
    engine = engine_for(service, [*lookup(), say("Is it right?"), *quote(), call("confirm_order"),
                                  say("Total 1580. Confirm?"), call("confirm_order"), say("Done.")],
                        delivery=delivery_for(service))
    engine.reply(order_id, "checkout, 12 street 5 phase 4")
    quoted = engine.reply(order_id, "haan")
    assert quoted["order"]["state"] == "READY_FOR_CONFIRMATION"  # confirm_order was blocked this turn
    done = engine.reply(order_id, "haan theek hai")
    assert done["order"]["state"] == "CONFIRMED" and done["order"]["total"] == "1580.00"


def test_any_change_after_the_quote_voids_the_confirmation(at_quote, order_id):
    engine = at_quote([call("remove_cart_item", line_id="nope"), call("confirm_order"), say("ok")])
    assert engine.reply(order_id, "yes")["order"]["state"] == "READY_FOR_CONFIRMATION"


def test_read_only_calls_do_not_void_the_confirmation(at_quote, order_id):
    engine = at_quote([call("get_cart"), call("confirm_order"), say("placed")])
    assert engine.reply(order_id, "yes")["order"]["state"] == "CONFIRMED"


def test_out_of_zone_lets_the_customer_try_another_address(service, order_id, delivery_for):
    engine = engine_for(service, [
        call("create_order"), call("resolve_address", raw="phase 8 street 9", area="phase 8", street="9"),
        say("Is it Street 9, Phase 8?"),
        *quote(), say("Sorry, that is too far. Another address?"),
        call("resolve_address", raw="12 street 5 phase 4", house="12", street="5", area="DHA Phase 4"),
        say("Is it House 12, Street 5, DHA Phase 4?"),
        *quote(), say("Great, total 1580. Confirm?")], delivery=delivery_for(service))
    engine.reply(order_id, "checkout, phase 8 street 9")
    assert engine.reply(order_id, "haan")["order"]["state"] == "OUT_OF_ZONE"
    retry = engine.reply(order_id, "nahi, mera address 12 street 5 phase 4 hai")
    assert retry["order"]["state"] == "COLLECTING_ADDRESS" and retry["order"]["address_confirmed"] is False
    assert engine.reply(order_id, "haan sahi")["order"]["state"] == "READY_FOR_CONFIRMATION"


def test_a_yes_does_not_choose_between_several_offered_places(service, order_id, delivery_for):
    engine = engine_for(service, [
        call("create_order"), call("resolve_address", raw="masjid", landmark="masjid"),
        say("Which one: 1 Jamia Masjid or 2 Madni Masjid?"),
        call("confirm_address", number=1), say("Which number?"),
        call("confirm_address", number=2), say("Saved.")], delivery=delivery_for(service))
    engine.reply(order_id, "checkout, masjid ke paas")
    assert engine.reply(order_id, "haan bilkul sahi hai")["order"]["address_confirmed"] is False
    assert engine.reply(order_id, "dusra wala")["order"]["address_confirmed"] is True


def test_tool_loop_is_stopped(service, order_id):
    engine = engine_for(service, [call("get_cart")] * 40)
    out = engine.reply(order_id, "hello")
    assert out["reply"] == FALLBACK_REPLY and out["order"]["state"] == "COLLECTING_ITEMS"


def test_falls_back_to_second_model_when_first_fails(service, order_id):
    engine = engine_for(service, [RuntimeError("groq down")], [say("Aap ka order ready hai.")])
    assert engine.reply(order_id, "hi")["reply"] == "Aap ka order ready hai."


def test_all_models_down_gives_a_safe_reply_and_leaves_the_order_alone(service, order_id):
    engine = engine_for(service, [RuntimeError("a")], [RuntimeError("b")])
    out = engine.reply(order_id, "hi")
    assert out["reply"] == FALLBACK_REPLY and out["order"]["state"] == "COLLECTING_ITEMS"


def test_gemini_style_content_blocks_are_flattened(service, order_id):
    engine = engine_for(service, [AIMessage(content=[{"type": "text", "text": "Assalam o alaikum"}])])
    assert engine.reply(order_id, "salam")["reply"] == "Assalam o alaikum"


def test_closed_order_never_reaches_the_model(service, order_id):
    service.transition(order_id, __import__("app.states", fromlist=["OrderState"]).OrderState.CANCELLED)
    model = ScriptedModel(script=[])
    out = ConversationEngine(service, [model]).reply(order_id, "hello")
    assert model.calls == 0 and "CANCELLED" in out["reply"]


def test_message_limits(service, order_id):
    engine = engine_for(service, [say("x")])
    for bad in ["", "   ", "a" * 501]:
        with pytest.raises(DomainError):
            engine.reply(order_id, bad)


def test_conversation_remembers_earlier_turns(service, order_id):
    model = ScriptedModel(script=[say("What size?"), say("Large, noted.")])
    engine = ConversationEngine(service, [model])
    engine.reply(order_id, "pizza chahiye")
    engine.reply(order_id, "large wali")
    history = engine.memory.get_tuple({"configurable": {"thread_id": str(order_id)}}).checkpoint["channel_values"]["messages"]
    assert [m.content for m in history if m.type == "human"] == ["pizza chahiye", "large wali"]


def test_chat_endpoint(settings):
    with TestClient(create_app(settings, models=[ScriptedModel(script=[say("Salam! Kya order karenge?")])])) as client:
        oid = client.post("/orders", headers={"Idempotency-Key": "chat-1"}).json()["id"]
        body = client.post(f"/orders/{oid}/chat", json={"message": "salam"}).json()
        assert body["reply"] == "Salam! Kya order karenge?" and body["order"]["id"] == oid
        assert client.post(f"/orders/{oid}/chat", json={"message": ""}).json()["error"]["code"] == "empty_message"


def test_chat_endpoint_without_a_model_is_503(settings):
    with TestClient(create_app(settings, models=[])) as client:
        oid = client.post("/orders", headers={"Idempotency-Key": "chat-2"}).json()["id"]
        resp = client.post(f"/orders/{oid}/chat", json={"message": "hi"})
        assert resp.status_code == 503 and resp.json()["error"]["code"] == "chat_unavailable"


def test_model_is_only_offered_tools_that_fit_the_order_state(service, order_id):
    model = ScriptedModel(script=[call("create_order"), say("Address?"), say("ok")], offered=[])
    engine = ConversationEngine(service, [model])
    engine.reply(order_id, "checkout")
    in_cart, in_checkout = model.offered[0], model.offered[1]
    assert "add_to_cart" in in_cart and "confirm_order" not in in_cart
    assert "add_to_cart" not in in_checkout and "resolve_address" in in_checkout
    assert "confirm_order" not in in_checkout


def test_repeated_sentences_from_a_glitchy_model_are_collapsed(service, order_id):
    glitch = "Coke ke liye variation bataiye.Coke ke liye variation bataiye. Coke ke liye variation bataiye."
    engine = engine_for(service, [say(glitch)])
    assert engine.reply(order_id, "coke")["reply"] == "Coke ke liye variation bataiye."


def test_menu_information_question_cannot_start_checkout(service, order_id):
    engine = engine_for(service, [call("create_order"), say("Super Max is a burger deal with ...")])
    out = engine.reply(order_id, "Super Max, what is it?")
    assert out["order"]["state"] == "COLLECTING_ITEMS"
    assert out["reply"].startswith("Super Max")


def test_explicit_order_intent_can_start_checkout(service, order_id):
    engine = engine_for(service, [call("create_order"), say("What address should I use?")])
    out = engine.reply(order_id, "I want to order this")
    assert out["order"]["state"] == "VALIDATING_ORDER"
