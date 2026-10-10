import uuid
from dataclasses import dataclass, field

from app.errors import DomainError, NotFound
from app.menu import Menu, MenuItem

MAX_LINE_QUANTITY = 20
MAX_LINES = 30

Selections = dict[int, list[int]]  # option group id -> chosen choice ids


@dataclass
class CartLine:
    item_id: int
    quantity: int
    selections: Selections = field(default_factory=dict)
    line_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])

    def to_json(self) -> dict:
        return {"line_id": self.line_id, "item_id": self.item_id, "quantity": self.quantity,
                "selections": {str(g): c for g, c in self.selections.items()}}

    @staticmethod
    def from_json(raw: dict) -> "CartLine":
        return CartLine(raw["item_id"], raw["quantity"],
                        {int(g): c for g, c in raw["selections"].items()}, raw["line_id"])


def normalize_selections(selections: Selections) -> Selections:
    return {g: sorted(c) for g, c in sorted(selections.items()) if c}


def validate_selections(item: MenuItem, selections: Selections) -> None:
    """Reject any option combination the menu does not allow. Raises DomainError."""
    groups = {g.id: g for g in item.groups}
    for gid, chosen in selections.items():
        if gid not in groups:
            raise DomainError("unknown_option_group", f"{item.name} has no option group {gid}")
        valid = {c.id for c in groups[gid].choices}
        for cid in chosen:
            if cid not in valid:
                raise DomainError("unknown_choice", f"choice {cid} does not belong to '{groups[gid].name}'")
        if len(set(chosen)) != len(chosen):
            raise DomainError("duplicate_choice", f"'{groups[gid].name}' lists the same choice twice")

    picked = {cid for chosen in selections.values() for cid in chosen}
    for group in item.groups:
        chosen = selections.get(group.id, [])
        # A choice tied to another choice (Cheese 150 only for the 6 inch size) exists
        # only when that parent choice was picked.
        active = [c for c in group.choices if c.linked_choice_id is None or c.linked_choice_id in picked]
        if group.linked_option_id is not None and not active:
            if chosen:
                raise DomainError("option_not_available", f"'{group.name}' does not apply to the current selection")
            continue
        active_ids = {c.id for c in active}
        for cid in chosen:
            if cid not in active_ids:
                raise DomainError("option_not_available",
                                  f"that '{group.name}' choice does not go with the other selections")
        if len(chosen) < group.min_selections:
            raise DomainError("missing_required_option", f"{item.name}: choose '{group.name}'")
        if len(chosen) > group.max_selections:
            raise DomainError("too_many_choices",
                              f"{item.name}: '{group.name}' allows at most {group.max_selections}")


def _check_quantity(quantity: int) -> None:
    if not isinstance(quantity, int) or isinstance(quantity, bool) or not 1 <= quantity <= MAX_LINE_QUANTITY:
        raise DomainError("invalid_quantity", f"quantity must be between 1 and {MAX_LINE_QUANTITY}")


def add_line(menu: Menu, lines: list[CartLine], item_id: int, quantity: int, selections: Selections) -> CartLine:
    item = menu.get(item_id)
    if item is None:
        raise NotFound("unknown_item", f"item {item_id} is not on the menu")
    if not item.available:
        raise DomainError("item_unavailable", f"{item.name} is not available")
    _check_quantity(quantity)
    selections = normalize_selections(selections)
    validate_selections(item, selections)
    for line in lines:
        if line.item_id == item_id and line.selections == selections:
            _check_quantity(line.quantity + quantity)
            line.quantity += quantity
            return line
    if len(lines) >= MAX_LINES:
        raise DomainError("cart_full", f"a cart holds at most {MAX_LINES} lines")
    line = CartLine(item_id, quantity, selections)
    lines.append(line)
    return line


def _find(lines: list[CartLine], line_id: str) -> CartLine:
    for line in lines:
        if line.line_id == line_id:
            return line
    raise NotFound("unknown_line", f"no cart line {line_id}")


def set_quantity(lines: list[CartLine], line_id: str, quantity: int) -> CartLine:
    line = _find(lines, line_id)
    _check_quantity(quantity)
    line.quantity = quantity
    return line


def remove_line(lines: list[CartLine], line_id: str) -> None:
    lines.remove(_find(lines, line_id))
