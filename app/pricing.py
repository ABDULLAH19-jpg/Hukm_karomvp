from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from app.cart import CartLine
from app.errors import DomainError
from app.menu import Menu, PriceBehavior

CENT = Decimal("0.01")


def money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class PricedLine:
    line_id: str
    item_id: int
    name: str
    quantity: int
    options: list[dict]  # [{group, choice, price}] for the receipt and the kitchen
    unit_price: Decimal
    line_total: Decimal


@dataclass(frozen=True)
class Pricing:
    lines: list[PricedLine]
    subtotal: Decimal
    delivery_fee: Decimal
    total: Decimal


def price_cart(menu: Menu, lines: list[CartLine], delivery_fee: Decimal = Decimal("0")) -> Pricing:
    """Every number here comes from the menu file. Nothing from the caller or a model is trusted."""
    priced: list[PricedLine] = []
    for line in lines:
        item = menu.get(line.item_id)
        if item is None or not item.available:
            raise DomainError("item_unavailable", f"item {line.item_id} is no longer orderable")
        base, replaced, options = item.price, False, []
        extras = Decimal("0")
        for group in item.groups:
            for cid in line.selections.get(group.id, []):
                choice = next(c for c in group.choices if c.id == cid)
                options.append({"group": group.name, "choice": choice.name, "price": str(money(choice.price))})
                if group.price_behavior is PriceBehavior.REPLACE:
                    if replaced:
                        raise DomainError("ambiguous_price", f"{item.name} has two size/variant prices selected")
                    base, replaced = choice.price, True
                else:
                    extras += choice.price
        unit = money(base + extras)
        priced.append(PricedLine(line.line_id, item.id, item.name, line.quantity, options, unit,
                                 money(unit * line.quantity)))
    subtotal = money(sum((p.line_total for p in priced), Decimal("0")))
    fee = money(delivery_fee)
    return Pricing(priced, subtotal, fee, money(subtotal + fee))
