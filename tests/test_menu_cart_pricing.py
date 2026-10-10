import json
from decimal import Decimal

import pytest

from app.cart import add_line, validate_selections
from app.errors import DomainError, MenuDataError
from app.menu import PriceBehavior, load_menu
from app.pricing import price_cart
from tests.conftest import PIZZA, item_named, pizza_selections


def codes(fn, *args):
    with pytest.raises(DomainError) as exc:
        fn(*args)
    return exc.value.code


def test_real_menu_loads_completely(menu):
    assert len(menu.items) == 103
    assert sum(len(i.groups) for i in menu.items.values()) == 165


def test_required_group_with_zero_min_max_is_still_required(menu):
    wings = item_named(menu, "Oven Baked Wings")
    group = wings.groups[0]
    assert (group.required, group.min_selections, group.max_selections) == (True, 1, 1)
    assert codes(validate_selections, wings, {}) == "missing_required_option"


def test_malformed_menu_fails_loudly(tmp_path):
    raw = json.loads(open("data/menu.json", encoding="utf-8").read())
    items = raw["menus"][0]["categories"][0]["subcategories"][0]["items"]
    items.append(dict(items[0]))  # duplicate item id
    bad = tmp_path / "menu.json"
    bad.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(MenuDataError, match="duplicate item id"):
        load_menu(bad)


def test_price_behavior_is_inferred_per_group(menu):
    size = next(g for g in item_named(menu, "Extreme Peri Crunch").groups if g.name == "Size")
    assert size.price_behavior is PriceBehavior.REPLACE
    solo = next(g for g in item_named(menu, "Chunk for One").groups if g.name == "Variation")
    assert solo.price_behavior is PriceBehavior.ADD  # Favourite 0 / Special +40


def test_rejects_unknown_item_choice_and_group(menu):
    pizza = item_named(menu, "Chicken Supreme")
    assert codes(add_line, menu, [], 999, 1, {}) == "unknown_item"
    assert codes(validate_selections, pizza, {1: [2]}) == "unknown_option_group"
    assert codes(validate_selections, pizza, {PIZZA["crust"]: [42]}) == "unknown_choice"


def test_rejects_option_that_belongs_to_a_different_size(menu):
    sel = pizza_selections(topping=False)
    sel[PIZZA["topping"]] = [PIZZA["cheese_small"]]  # 6 inch cheese on a 9 inch pizza
    assert codes(add_line, menu, [], item_named(menu, "Chicken Supreme").id, 1, sel) == "option_not_available"


def test_rejects_missing_required_and_too_many(menu):
    pizza = item_named(menu, "Chicken Supreme")
    assert codes(validate_selections, pizza, {}) == "missing_required_option"
    deep, stuff = 1400148, 1400149
    assert codes(validate_selections, pizza, {PIZZA["crust"]: [deep, stuff]}) == "too_many_choices"


@pytest.mark.parametrize("qty", [0, -1, 21, True])
def test_rejects_bad_quantity(menu, qty):
    assert codes(add_line, menu, [], item_named(menu, "Chicken Supreme").id, qty, pizza_selections()) == "invalid_quantity"


def test_multi_select_group_enforces_exact_count(menu):
    box = item_named(menu, "Party Box")
    group = next(g for g in box.groups if g.name == "Fried Burgers")
    assert (group.min_selections, group.max_selections) == (2, 2)
    one = [group.choices[0].id]
    assert codes(validate_selections, box, {group.id: one}) == "missing_required_option"


def test_price_comes_only_from_menu_and_is_deterministic(menu):
    lines = []
    add_line(menu, lines, item_named(menu, "Chicken Supreme").id, 2, pizza_selections())
    first = price_cart(menu, lines)
    assert first.lines[0].unit_price == Decimal("1580.00")  # 1330 replaced base + 250 topping
    assert first.total == Decimal("3160.00")
    assert price_cart(menu, lines) == first


def test_identical_lines_merge_and_total_follows_quantity(menu):
    lines = []
    pid = item_named(menu, "Chicken Supreme").id
    add_line(menu, lines, pid, 1, pizza_selections())
    add_line(menu, lines, pid, 2, pizza_selections())
    assert len(lines) == 1 and lines[0].quantity == 3
    assert price_cart(menu, lines, Decimal("150")).total == Decimal("4890.00")  # 3 * 1580 + fee
