import json
import re
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from pathlib import Path

from app.errors import MenuDataError


class PriceBehavior(str, Enum):
    REPLACE = "replace"  # chosen choice price is the item price (size variants)
    ADD = "add"  # chosen choice price is a surcharge on top


@dataclass(frozen=True)
class Choice:
    id: int
    name: str
    price: Decimal
    linked_choice_id: int | None


@dataclass(frozen=True)
class OptionGroup:
    id: int
    name: str
    required: bool
    multiple: bool
    min_selections: int
    max_selections: int
    price_behavior: PriceBehavior
    linked_option_id: int | None
    choices: tuple[Choice, ...]


@dataclass(frozen=True)
class MenuItem:
    id: int
    name: str
    description: str
    price: Decimal
    available: bool
    category: str
    subcategory: str
    groups: tuple[OptionGroup, ...]


def _price(value, where: str) -> Decimal:
    try:
        price = Decimal(str(value))
    except Exception:
        raise MenuDataError(f"{where}: price {value!r} is not a number")
    if price < 0:
        raise MenuDataError(f"{where}: negative price {price}")
    return price


def _normalize_group(raw: dict, item_name: str) -> OptionGroup:
    where = f"item {item_name!r} group {raw.get('id')}"
    if not raw.get("choices"):
        raise MenuDataError(f"{where}: no choices")
    choices = tuple(
        Choice(c["id"], c["name"], _price(c["price"], f"{where} choice {c['id']}"), c.get("linkedChoiceId"))
        for c in raw["choices"]
    )
    required, multiple = bool(raw["required"]), bool(raw["multiple"])
    # The export writes min/max as 0/0 even for required groups, so 0 never means "optional".
    if multiple:
        low = max(raw["minSelections"], 1 if required else 0)
        high = raw["maxSelections"] or len(choices)
    else:
        low, high = (1 if required else 0), 1
    if low > high:
        raise MenuDataError(f"{where}: min {low} exceeds max {high}")
    # Full-price variants have every choice priced; surcharge groups include free choices
    # (Favourite 0 / Special +40) or are optional add-ons.
    behavior = PriceBehavior.REPLACE if required and all(c.price > 0 for c in choices) else PriceBehavior.ADD
    return OptionGroup(raw["id"], raw["name"], required, multiple, low, high, behavior, raw.get("linkedOptionId"), choices)


class Menu:
    def __init__(self, items: list[MenuItem]):
        self.items = {i.id: i for i in items}

    def get(self, item_id: int) -> MenuItem | None:
        return self.items.get(item_id)

    def search(self, query: str, limit: int = 10) -> list[MenuItem]:
        tokens = re.findall(r"\w+", query.lower())
        if not tokens:
            return []
        scored = []
        for item in self.items.values():
            name = item.name.lower()
            haystack = f"{name} {item.category.lower()} {item.subcategory.lower()}"
            hits = sum(t in haystack for t in tokens)
            if hits:
                # exact-name match first, then more matched tokens, then shorter names
                scored.append((name != " ".join(tokens), -hits, len(name), item))
        scored.sort(key=lambda s: s[:3])
        return [s[3] for s in scored[:limit]]


def load_menu(path: str | Path) -> Menu:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not data.get("menus"):
        raise MenuDataError("menu file has no menus")
    items: list[MenuItem] = []
    seen_items: set[int] = set()
    seen_groups: set[int] = set()
    seen_choices: set[int] = set()
    for menu in data["menus"]:
        for cat in menu["categories"]:
            for sub in cat["subcategories"]:
                for raw in sub["items"]:
                    where = f"item {raw.get('id')} {raw.get('name')!r}"
                    if raw["id"] in seen_items:
                        raise MenuDataError(f"{where}: duplicate item id")
                    seen_items.add(raw["id"])
                    if not isinstance(raw["available"], bool):
                        raise MenuDataError(f"{where}: invalid availability {raw['available']!r}")
                    groups = tuple(_normalize_group(g, raw["name"]) for g in raw.get("options", []))
                    for g in groups:
                        if g.id in seen_groups:
                            raise MenuDataError(f"{where}: duplicate option group id {g.id}")
                        seen_groups.add(g.id)
                        for c in g.choices:
                            if c.id in seen_choices:
                                raise MenuDataError(f"{where}: duplicate choice id {c.id}")
                            seen_choices.add(c.id)
                    items.append(MenuItem(
                        raw["id"], raw["name"], raw.get("description") or "",
                        _price(raw["price"], where), raw["available"],
                        cat["name"], sub["name"], groups,
                    ))
    return Menu(items)
