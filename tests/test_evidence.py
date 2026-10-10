import pytest

from app.db import make_session_factory
from app.errors import DomainError
from app.evidence import check_confirm
from app.orders import OrderService
from app.tools import build_tools
from tests.conftest import PIZZA, item_named


@pytest.fixture
def setup(menu, db_url):
    service = OrderService(make_session_factory(db_url), menu)
    order, _ = service.create("evidence")
    said: list[str] = []
    tools = {t.name: t for t in build_tools(service, order["id"], heard=lambda: said)}
    return service, tools, said, order["id"]


def coke_group(menu):
    coke = item_named(menu, "Coke")
    group = coke.groups[0]
    return coke, group, {c.name: c.id for c in group.choices}


def add(tools, item, group, choice_id, words):
    return tools["add_to_cart"].invoke({"item_id": item.id, "quantity": 1, "selections": [
        {"group_id": group.id, "choice_ids": [choice_id], "customer_words": words}]})


def test_model_cannot_pick_an_option_the_customer_never_named(menu, setup):
    _, tools, said, _ = setup
    coke, group, ids = coke_group(menu)
    said += ["aur 2 coke bhi", "nahi aik hi kar do coke"]  # the exact live-run failure
    result = add(tools, coke, group, ids["500 Ml Coke"], "coke")
    assert result["ok"] is False and result["error"] == "option_not_chosen"
    assert "500 Ml Coke" in result["message"]  # the error tells the model what to ask


def test_no_quote_means_no_add(menu, setup):
    _, tools, said, _ = setup
    coke, group, ids = coke_group(menu)
    said.append("coke chahiye")
    assert add(tools, coke, group, ids["500 Ml Coke"], "")["error"] == "option_not_chosen"


def test_invented_quote_is_rejected(menu, setup):
    _, tools, said, _ = setup
    coke, group, ids = coke_group(menu)
    said.append("coke chahiye")
    assert add(tools, coke, group, ids["500 Ml Coke"], "500 ml")["error"] == "option_not_chosen"


def test_real_quote_is_accepted(menu, setup):
    service, tools, said, oid = setup
    coke, group, ids = coke_group(menu)
    said.append("coke 500 ml wali")
    assert add(tools, coke, group, ids["500 Ml Coke"], "500 ml")["ok"] is True
    assert service.get(oid)["subtotal"] == "130.00"


def test_quote_for_one_size_cannot_justify_another(menu, setup):
    _, tools, said, _ = setup
    coke, group, ids = coke_group(menu)
    said.append("coke 500 ml wali")
    assert add(tools, coke, group, ids["350 Ml Coke"], "500 ml")["error"] == "option_not_chosen"


def test_roman_urdu_size_words_count(menu, setup):
    _, tools, said, _ = setup
    peri = item_named(menu, "Extreme Peri Crunch")
    size = next(g for g in peri.groups if g.name == "Size")
    large = next(c for c in size.choices if "Large" in c.name)
    said.append("bara wali")
    assert add(tools, peri, size, large.id, "bara wali")["ok"] is True


def test_yes_to_a_listed_option_counts(menu, setup):
    _, tools, said, _ = setup
    coke, group, ids = coke_group(menu)
    said.append("haan")
    assert add(tools, coke, group, ids["500 Ml Coke"], "haan")["ok"] is True


def test_same_named_choices_defer_to_server_dependency_rules(menu, setup):
    _, tools, said, _ = setup
    said.append("deep pan, regular 9 inch, extra cheese")
    pizza = item_named(menu, "Chicken Supreme")
    sel = [{"group_id": PIZZA["crust"], "choice_ids": [PIZZA["deep_pan"]], "customer_words": "deep pan"},
           {"group_id": PIZZA["size"], "choice_ids": [PIZZA["regular"]], "customer_words": "regular 9 inch"},
           {"group_id": PIZZA["topping"], "choice_ids": [PIZZA["cheese_small"]], "customer_words": "extra cheese"}]
    result = tools["add_to_cart"].invoke({"item_id": pizza.id, "selections": sel})
    assert result["error"] == "option_not_available"  # wrong-size cheese still stopped by the cart rules


def test_without_a_heard_source_the_guard_is_off(menu, db_url):
    service = OrderService(make_session_factory(db_url), menu)
    order, _ = service.create("noguard")
    tools = {t.name: t for t in build_tools(service, order["id"])}
    coke, group, ids = coke_group(menu)
    assert add(tools, coke, group, ids["500 Ml Coke"], "")["ok"] is True


# ---- picking between several offered places ---------------------------------------------

@pytest.fixture
def pick(menu, db_url, delivery_for):
    """Order with two masjid places offered, and the tool set that checks what the customer said."""
    service = OrderService(make_session_factory(db_url), menu)
    order, _ = service.create("pick")
    service.add_item(order["id"], item_named(menu, "Chicken Supreme").id, 1,
                     {PIZZA["crust"]: [PIZZA["deep_pan"]], PIZZA["size"]: [PIZZA["regular"]]})
    service.transition(order["id"], __import__("app.states", fromlist=["OrderState"]).OrderState.VALIDATING_ORDER)
    said: list[str] = []
    tools = {t.name: t for t in build_tools(service, order["id"], delivery=delivery_for(service), heard=lambda: said)}
    tools["resolve_address"].invoke({"raw": "masjid", "landmark": "masjid"})
    return tools, said


def confirm(tools, number):
    return tools["confirm_address"].invoke({"number": number})


def test_yes_does_not_pick_between_two_places(pick):
    tools, said = pick
    said.append("haan bilkul sahi hai")
    result = confirm(tools, 1)
    assert result["error"] == "place_not_chosen" and "Madni" in result["message"]


@pytest.mark.parametrize("words,number,ok", [
    ("pehla wala", 1, True), ("number 2", 2, True), ("the second one", 2, True),
    ("madni wali", 2, True), ("jamia", 1, True),
    ("2", 1, False),  # said two, model picked one
    ("madni wali", 1, False),
])
def test_customer_has_to_name_the_place(pick, words, number, ok):
    tools, said = pick
    said.append(words)
    assert confirm(tools, number)["ok"] is ok


def test_a_single_offered_place_needs_only_a_yes(menu, db_url, delivery_for):
    service = OrderService(make_session_factory(db_url), menu)
    order, _ = service.create("single")
    service.add_item(order["id"], item_named(menu, "Chicken Supreme").id, 1,
                     {PIZZA["crust"]: [PIZZA["deep_pan"]], PIZZA["size"]: [PIZZA["regular"]]})
    service.transition(order["id"], __import__("app.states", fromlist=["OrderState"]).OrderState.VALIDATING_ORDER)
    tools = {t.name: t for t in build_tools(service, order["id"], delivery=delivery_for(service), heard=lambda: ["haan"])}
    tools["resolve_address"].invoke({"raw": "phase 4 mein 12 street 5", "house": "12", "area": "DHA Phase 4"})
    assert confirm(tools, 1)["ok"] is True


# ---- check_confirm: the customer must affirm the quoted total ---------------


def test_check_confirm_passes_for_clear_yes():
    check_confirm("yes")
    check_confirm("haan bilkul theek hai")
    check_confirm("ok confirm kar do")
    check_confirm("done")
    check_confirm("perfect")


def test_check_confirm_fails_for_place_order():
    """'place order' does not contain a confirmation word (we removed 'place' from the set)."""
    with pytest.raises(DomainError) as exc:
        check_confirm("place order please")
    assert exc.value.code == "confirmation_needed"


def test_check_confirm_fails_for_empty_or_irrelevant():
    with pytest.raises(DomainError):
        check_confirm("")
    with pytest.raises(DomainError):
        check_confirm("how much is delivery")
    with pytest.raises(DomainError):
        check_confirm("what's the total")


def test_check_confirm_passes_for_roman_urdu():
    check_confirm("haan")
    check_confirm("ji bilkul")
    check_confirm("theek hai")
    check_confirm("sahi")
    check_confirm("zabardast kar do")
