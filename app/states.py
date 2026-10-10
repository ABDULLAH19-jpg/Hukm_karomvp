from enum import Enum

from app.errors import Conflict


class OrderState(str, Enum):
    DRAFT = "DRAFT"
    COLLECTING_ITEMS = "COLLECTING_ITEMS"
    VALIDATING_ORDER = "VALIDATING_ORDER"
    COLLECTING_ADDRESS = "COLLECTING_ADDRESS"
    CHECKING_DELIVERY = "CHECKING_DELIVERY"
    READY_FOR_CONFIRMATION = "READY_FOR_CONFIRMATION"
    CONFIRMED = "CONFIRMED"
    SUBMITTED_TO_RESTAURANT = "SUBMITTED_TO_RESTAURANT"
    PREPARING = "PREPARING"
    READY = "READY"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    # The kitchen looked at a confirmed order and refused it. Distinct from CANCELLED, which is the
    # customer backing out, so the two can be told apart in the numbers.
    REJECTED = "REJECTED"
    OUT_OF_ZONE = "OUT_OF_ZONE"
    PICKUP_SELECTED = "PICKUP_SELECTED"
    HANDOFF_REQUIRED = "HANDOFF_REQUIRED"
    FAILED = "FAILED"


S = OrderState

# States where the cart can still be edited.
EDITABLE = {S.DRAFT, S.COLLECTING_ITEMS}

# Before the customer confirms, they can always reopen the cart to change it.
_PRE_CONFIRM = [S.VALIDATING_ORDER, S.COLLECTING_ADDRESS, S.CHECKING_DELIVERY, S.READY_FOR_CONFIRMATION]

TRANSITIONS: dict[OrderState, set[OrderState]] = {
    S.DRAFT: {S.COLLECTING_ITEMS, S.CANCELLED, S.FAILED},
    S.COLLECTING_ITEMS: {S.VALIDATING_ORDER, S.CANCELLED, S.HANDOFF_REQUIRED, S.FAILED},
    S.VALIDATING_ORDER: {S.COLLECTING_ADDRESS, S.COLLECTING_ITEMS, S.CANCELLED, S.HANDOFF_REQUIRED, S.FAILED},
    S.COLLECTING_ADDRESS: {S.CHECKING_DELIVERY, S.COLLECTING_ITEMS, S.CANCELLED, S.HANDOFF_REQUIRED, S.FAILED},
    S.CHECKING_DELIVERY: {S.READY_FOR_CONFIRMATION, S.COLLECTING_ADDRESS, S.COLLECTING_ITEMS, S.OUT_OF_ZONE,
                          S.CANCELLED, S.HANDOFF_REQUIRED, S.FAILED},
    S.READY_FOR_CONFIRMATION: {S.CONFIRMED, S.COLLECTING_ADDRESS, S.COLLECTING_ITEMS, S.CANCELLED,
                               S.HANDOFF_REQUIRED, S.FAILED},
    S.CONFIRMED: {S.SUBMITTED_TO_RESTAURANT, S.CANCELLED, S.HANDOFF_REQUIRED, S.FAILED},
    S.SUBMITTED_TO_RESTAURANT: {S.PREPARING, S.REJECTED, S.CANCELLED, S.HANDOFF_REQUIRED, S.FAILED},
    S.PREPARING: {S.READY, S.REJECTED, S.CANCELLED, S.HANDOFF_REQUIRED, S.FAILED},
    S.READY: {S.COMPLETED, S.CANCELLED, S.HANDOFF_REQUIRED},
    # Staff resolve a handoff by either dropping the order or putting it back on the happy path.
    S.HANDOFF_REQUIRED: {S.COLLECTING_ITEMS, S.CONFIRMED, S.CANCELLED, S.FAILED},
    S.COMPLETED: set(),
    S.CANCELLED: set(),
    S.REJECTED: set(),
    # A wrong address match must not cost the customer the order: they can give another address or cancel.
    S.OUT_OF_ZONE: {S.COLLECTING_ADDRESS, S.PICKUP_SELECTED, S.CANCELLED, S.HANDOFF_REQUIRED},
    S.PICKUP_SELECTED: {S.READY_FOR_CONFIRMATION, S.COLLECTING_ITEMS, S.CANCELLED, S.HANDOFF_REQUIRED},
    S.FAILED: set(),
}

TERMINAL = {s for s, nxt in TRANSITIONS.items() if not nxt}


def check_transition(current: OrderState, target: OrderState) -> None:
    if target not in TRANSITIONS[current]:
        raise Conflict("invalid_transition", f"cannot move an order from {current.value} to {target.value}")


# ---- the kitchen board -----------------------------------------------------
# One source of truth. The API serves these columns and actions and the frontend renders whatever it
# is handed, so no state reaches a column, and no button reaches a target, without being listed here
# and checked by tests/test_board.py.

# Letting the board fall back to a default column for an unlisted state is what put unconfirmed
# orders in front of the kitchen, so there is no fallback: a state is on the board or it is not.
KITCHEN_COLUMNS: tuple[tuple[str, str, tuple[OrderState, ...]], ...] = (
    ("new", "New", (S.CONFIRMED, S.SUBMITTED_TO_RESTAURANT)),
    ("handoff", "Needs a person", (S.HANDOFF_REQUIRED,)),
    ("preparing", "Preparing", (S.PREPARING,)),
    ("ready", "Ready", (S.READY,)),
    ("rejected", "Rejected", (S.REJECTED,)),
    ("completed", "Completed", (S.COMPLETED,)),
)

BOARD_STATES = frozenset(state for _, _, states in KITCHEN_COLUMNS for state in states)

# Every button the board may offer and the move it makes. A card the kitchen cannot act on and
# cannot finish is a dead end, so each non-terminal board state appears here with at least one move.
KITCHEN_ACTIONS: dict[OrderState, tuple[tuple[str, OrderState, str], ...]] = {
    S.CONFIRMED: (("Accept order", S.SUBMITTED_TO_RESTAURANT, "primary"),),
    S.SUBMITTED_TO_RESTAURANT: (("Start preparing", S.PREPARING, "primary"),
                                ("Reject", S.REJECTED, "danger")),
    S.PREPARING: (("Mark ready", S.READY, "primary"), ("Reject", S.REJECTED, "danger")),
    S.READY: (("Complete", S.COMPLETED, "primary"),),
    # Staff resolve a handoff by putting the order back on its path, or by dropping it. The reason
    # the order was handed off is in the delivery row and in the order history.
    S.HANDOFF_REQUIRED: (("Back to the cart", S.COLLECTING_ITEMS, "quiet"),
                         ("Place as confirmed", S.CONFIRMED, "primary"),
                         ("Cancel", S.CANCELLED, "danger"),
                         ("Mark failed", S.FAILED, "danger")),
}

# Live conversations. These never reach the board: the customer may still change or abandon the
# order, and the kitchen cooking one nobody confirmed costs money and trust.
CONVERSATION_STATES = frozenset({S.DRAFT, S.COLLECTING_ITEMS, S.VALIDATING_ORDER, S.COLLECTING_ADDRESS,
                                S.CHECKING_DELIVERY, S.READY_FOR_CONFIRMATION, S.OUT_OF_ZONE,
                                S.PICKUP_SELECTED})
