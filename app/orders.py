from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.orm.exc import StaleDataError

from app import cart as cart_engine
from app.cart import CartLine, Selections
from app.db import OrderEventRow, OrderRow
from app.errors import Conflict, DomainError, NotFound
from app.logging_setup import logger
from app.menu import Menu
from app.pricing import Pricing, money, price_cart
from app.schedule import Schedule
from app.states import BOARD_STATES, EDITABLE, TERMINAL, OrderState, check_transition

S = OrderState

# How long a completed order stays on the board, so the shift can still see what just went out.
COMPLETED_WINDOW_H = 12


def _as_utc(when: datetime | None) -> datetime | None:
    """SQLite hands datetimes back naive. They were written as UTC, so read them as UTC."""
    if when is None or when.tzinfo is not None:
        return when
    return when.replace(tzinfo=timezone.utc)


class OrderService:
    def __init__(self, sessions: sessionmaker, menu: Menu, delivery_fee: Decimal = Decimal("0"),
                 schedule: Schedule | None = None):
        self.sessions = sessions
        self.menu = menu
        self.delivery_fee = delivery_fee
        self.schedule = schedule

    # ---- reads -------------------------------------------------------------

    def get(self, order_id: int) -> dict:
        with self.sessions() as s:
            return self._view(self._row(s, order_id))

    def list_active(self) -> list[dict]:
        """Every order that has not finished, newest first. Includes customers still deciding.

        The voice demo polls this to show the caller their own order. The kitchen wants board().
        """
        terminal = {s.value for s in TERMINAL}
        with self.sessions() as s:
            rows = s.scalars(select(OrderRow).where(OrderRow.state.not_in(terminal))
                             .order_by(OrderRow.created_at.desc())).all()
            return [self._view(r) for r in rows]

    def board(self, completed_within_h: int = COMPLETED_WINDOW_H) -> list[dict]:
        """The kitchen board: the states the kitchen can work on, newest first.

        Only BOARD_STATES qualify, so a live conversation cannot reach the board and the kitchen
        cannot start cooking an order the customer has not confirmed. Completed orders stay for a
        short window so the shift can see what just went out.
        """
        states = [s.value for s in BOARD_STATES] + [S.COMPLETED.value]
        cutoff = datetime.now(timezone.utc) - timedelta(hours=completed_within_h)
        with self.sessions() as s:
            rows = s.scalars(select(OrderRow).where(OrderRow.state.in_(states))
                             .order_by(OrderRow.created_at.desc())).all()
        views = [self._view(r) for r in rows if self._on_board(r, cutoff)]
        stuck = [v["id"] for v in views if v["state"] == S.HANDOFF_REQUIRED.value]
        if stuck:  # staff need the reason on the card, not one click away
            reasons = self._latest_reasons(stuck)
            for view in views:
                if view["id"] in reasons:
                    view["note"] = reasons[view["id"]]
        return views

    @staticmethod
    def _on_board(row: OrderRow, cutoff: datetime) -> bool:
        if row.state != S.COMPLETED.value:
            return True
        finished = _as_utc(row.updated_at)
        return finished is None or finished >= cutoff

    def _latest_reasons(self, order_ids: list[int]) -> dict[int, str]:
        """The most recent recorded reason per order, for the cards that have to explain themselves."""
        with self.sessions() as s:
            rows = s.scalars(select(OrderEventRow).where(OrderEventRow.order_id.in_(order_ids),
                                                        OrderEventRow.reason.is_not(None))
                             .order_by(OrderEventRow.id.desc())).all()
        reasons: dict[int, str] = {}
        for event in rows:  # newest first, so the first reason seen for an order is its latest
            reasons.setdefault(event.order_id, event.reason)
        return reasons

    def history(self, order_id: int) -> list[dict]:
        with self.sessions() as s:
            self._row(s, order_id)
            events = s.scalars(select(OrderEventRow).where(OrderEventRow.order_id == order_id)
                               .order_by(OrderEventRow.id)).all()
            return [{"from": e.from_state, "to": e.to_state, "reason": e.reason, "at": e.at.isoformat()}
                    for e in events]

    # ---- writes ------------------------------------------------------------

    def create(self, idempotency_key: str) -> tuple[dict, bool]:
        """Returns (order, created). The same key always returns the same order."""
        key = idempotency_key.strip()
        if not key or len(key) > 128:
            raise DomainError("invalid_idempotency_key", "idempotency key must be 1 to 128 characters")
        for _ in range(2):
            try:
                with self.sessions.begin() as s:
                    existing = s.scalar(select(OrderRow).where(OrderRow.idempotency_key == key))
                    if existing:
                        return self._view(existing), False
                    row = OrderRow(idempotency_key=key, state=S.DRAFT.value, lines=[])
                    s.add(row)
                    s.flush()
                    row.public_number = f"HK-{row.id:05d}"
                    s.add(OrderEventRow(order_id=row.id, from_state=None, to_state=S.DRAFT.value))
                    s.flush()
                    return self._view(row), True
            except IntegrityError:
                continue  # a concurrent request with the same key won the insert; read theirs
        raise Conflict("create_conflict", "could not create the order, retry the request")

    def add_item(self, order_id: int, item_id: int, quantity: int, selections: Selections) -> dict:
        def apply(s: Session, row: OrderRow):
            self._require_editable(row)
            lines = self._lines(row)
            cart_engine.add_line(self.menu, lines, item_id, quantity, selections)
            row.lines = [l.to_json() for l in lines]
            if row.state == S.DRAFT.value:
                self._move(s, row, S.COLLECTING_ITEMS, "first item added")
        return self._mutate(order_id, apply)

    def set_quantity(self, order_id: int, line_id: str, quantity: int) -> dict:
        def apply(s: Session, row: OrderRow):
            self._require_editable(row)
            lines = self._lines(row)
            cart_engine.set_quantity(lines, line_id, quantity)
            row.lines = [l.to_json() for l in lines]
        return self._mutate(order_id, apply)

    def remove_item(self, order_id: int, line_id: str) -> dict:
        def apply(s: Session, row: OrderRow):
            self._require_editable(row)
            lines = self._lines(row)
            cart_engine.remove_line(lines, line_id)
            row.lines = [l.to_json() for l in lines]
        return self._mutate(order_id, apply)

    def begin_address(self, order_id: int, raw: str, candidates: list[dict]) -> dict:
        """Store what the customer said and the places the server found. Nothing is confirmed yet."""
        def apply(s: Session, row: OrderRow):
            if row.state != S.COLLECTING_ADDRESS.value:
                if row.state not in {S.VALIDATING_ORDER.value, S.CHECKING_DELIVERY.value,
                                     S.READY_FOR_CONFIRMATION.value, S.OUT_OF_ZONE.value}:
                    raise Conflict("wrong_state", f"address cannot be set while order is {row.state}")
                self._move(s, row, S.COLLECTING_ADDRESS, "address being set")
            row.address_text = raw[:500]
            row.address_candidates = candidates or None
            row.address_label = row.address_lat = row.address_lon = row.address_precision = None
            row.address_place_id = row.address_confidence = None
            row.route_distance_m = row.delivery_allowed = None
        return self._mutate(order_id, apply)

    def set_branch(self, order_id: int, branch_id: str, name: str, radius_m: int) -> dict:
        def apply(s: Session, row: OrderRow):
            row.branch_id, row.branch_name, row.branch_radius_m = branch_id, name, radius_m
        return self._mutate(order_id, apply)

    def address_pending(self, order_id: int) -> bool:
        with self.sessions() as s:
            row = self._row(s, order_id)
            return bool(row.address_candidates) and row.state == S.COLLECTING_ADDRESS.value

    def confirm_address(self, order_id: int, number: int) -> dict:
        """The customer picked one of the candidates the server stored. Only those can be chosen."""
        def apply(s: Session, row: OrderRow):
            if row.state != S.COLLECTING_ADDRESS.value or not row.address_candidates:
                raise Conflict("no_address_to_confirm", "there is no address waiting for confirmation")
            if not isinstance(number, int) or not 1 <= number <= len(row.address_candidates):
                raise DomainError("unknown_candidate", f"choose a number from 1 to {len(row.address_candidates)}")
            chosen = row.address_candidates[number - 1]
            row.address_label, row.address_lat, row.address_lon = chosen["label"], chosen["lat"], chosen["lon"]
            row.address_precision = chosen["precision"]
            row.address_place_id = chosen.get("place_id")
            row.address_confidence = chosen.get("confidence")
            row.address_candidates = None
        return self._mutate(order_id, apply)

    def address_candidate_labels(self, order_id: int) -> list[str]:
        with self.sessions() as s:
            return [c["label"] for c in (self._row(s, order_id).address_candidates or [])]

    def delivery_target(self, order_id: int) -> dict:
        with self.sessions() as s:
            row = self._row(s, order_id)
            if row.address_lat is None:
                raise Conflict("address_not_confirmed", "the customer has not confirmed an address yet")
            return {"lat": row.address_lat, "lon": row.address_lon, "precision": row.address_precision,
                    "confidence": row.address_confidence, "state": row.state,
                    "branch_id": row.branch_id, "branch_radius_m": row.branch_radius_m}

    def begin_delivery_check(self, order_id: int) -> dict:
        def apply(s: Session, row: OrderRow):
            if row.state == S.CHECKING_DELIVERY.value:
                return  # retry after a map outage
            self._move(s, row, S.CHECKING_DELIVERY, "checking the delivery zone")
        return self._mutate(order_id, apply)

    def finish_delivery(self, order_id: int, distance_m: int, allowed: bool, handoff_reason: str | None = None) -> dict:
        def apply(s: Session, row: OrderRow):
            row.route_distance_m, row.delivery_allowed = distance_m, allowed
            if handoff_reason:
                self._move(s, row, S.HANDOFF_REQUIRED, handoff_reason)
            elif allowed:
                self._move(s, row, S.READY_FOR_CONFIRMATION, f"inside the zone, {distance_m} m by road")
            else:
                self._move(s, row, S.OUT_OF_ZONE, f"outside the zone, {distance_m} m")
        return self._mutate(order_id, apply)

    def transition(self, order_id: int, target: OrderState, reason: str | None = None) -> dict:
        def apply(s: Session, row: OrderRow):
            if row.state == target.value:
                return  # a retried request, nothing to do
            self._move(s, row, target, reason)
        return self._mutate(order_id, apply)

    # ---- internals ---------------------------------------------------------

    def _mutate(self, order_id: int, apply) -> dict:
        try:
            with self.sessions.begin() as s:
                row = self._row(s, order_id)
                apply(s, row)
                s.flush()
                return self._view(row)
        except StaleDataError:
            raise Conflict("concurrent_update", "the order changed while handling this request, retry")

    @staticmethod
    def _row(s: Session, order_id: int) -> OrderRow:
        row = s.get(OrderRow, order_id)
        if row is None:
            raise NotFound("unknown_order", f"order {order_id} does not exist")
        return row

    @staticmethod
    def _lines(row: OrderRow) -> list[CartLine]:
        return [CartLine.from_json(l) for l in row.lines]

    @staticmethod
    def _require_editable(row: OrderRow) -> None:
        if OrderState(row.state) not in EDITABLE:
            raise Conflict("order_locked", f"items cannot change while order is {row.state}")

    def _require_open(self) -> None:
        if self.schedule is None:
            return
        status, reason = self.schedule.status()
        if status != "open":
            raise DomainError("restaurant_closed", reason)

    def _price(self, row: OrderRow) -> Pricing:
        lines = self._lines(row)
        for line in lines:  # menu data may have changed since the item was added
            item = self.menu.get(line.item_id)
            if item is None:
                raise DomainError("unknown_item", f"item {line.item_id} is no longer on the menu")
            cart_engine.validate_selections(item, line.selections)
        return price_cart(self.menu, lines, self.delivery_fee)

    def _move(self, s: Session, row: OrderRow, target: OrderState, reason: str | None) -> None:
        current = OrderState(row.state)
        check_transition(current, target)

        if target in (S.VALIDATING_ORDER, S.CONFIRMED):
            self._require_open()

        if target is S.VALIDATING_ORDER:
            if not row.lines:
                raise DomainError("empty_order", "add at least one item before checking out")
            quote = self._price(row)
            row.subtotal, row.delivery_fee, row.total = quote.subtotal, quote.delivery_fee, quote.total
            row.snapshot = [self._line_dict(p) for p in quote.lines]
        elif target is S.CHECKING_DELIVERY and row.address_lat is None:
            raise DomainError("address_required", "the customer must confirm a delivery address first")
        elif target is S.READY_FOR_CONFIRMATION and current is not S.PICKUP_SELECTED and row.delivery_allowed is not True:
            raise Conflict("delivery_not_approved", "the delivery zone check has not approved this address")
        elif target is S.OUT_OF_ZONE and row.delivery_allowed is not False:
            raise Conflict("delivery_not_rejected", "the delivery zone check did not reject this address")
        elif target is S.CONFIRMED and current is S.READY_FOR_CONFIRMATION:
            quote = self._price(row)
            if money(quote.total) != money(row.total or Decimal("0")):
                raise Conflict("price_changed", "the total changed since it was quoted, re-validate the order")
        elif target is S.COLLECTING_ITEMS:
            row.subtotal = row.delivery_fee = row.total = row.snapshot = None

        s.add(OrderEventRow(order_id=row.id, from_state=current.value, to_state=target.value, reason=reason))
        row.state = target.value
        logger.info("order_transition", extra={"order_id": row.id, "from": current.value, "to": target.value,
                                               "reason": reason})

    @staticmethod
    def _line_dict(p) -> dict:
        return {"line_id": p.line_id, "item_id": p.item_id, "name": p.name, "quantity": p.quantity,
                "options": p.options, "unit_price": str(p.unit_price), "line_total": str(p.line_total)}

    def _view(self, row: OrderRow) -> dict:
        view = {"id": row.id, "number": row.public_number, "state": row.state,
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "updated_at": row.updated_at.isoformat() if row.updated_at else None,
                "address": row.address_label or row.address_text, "address_confirmed": row.address_lat is not None,
                "address_precision": row.address_precision, "address_place_id": row.address_place_id,
                "address_confidence": row.address_confidence, "distance_m": row.route_distance_m,
                "branch": {"id": row.branch_id, "name": row.branch_name, "radius_m": row.branch_radius_m},
                "lines": [], "subtotal": None, "delivery_fee": None, "total": None, "issues": []}
        if row.snapshot is not None:
            view.update(lines=row.snapshot, subtotal=str(money(row.subtotal)),
                        delivery_fee=str(money(row.delivery_fee)), total=str(money(row.total)))
            return view
        try:
            quote = self._price(row)
            view.update(lines=[self._line_dict(p) for p in quote.lines], subtotal=str(quote.subtotal))
        except DomainError as exc:
            view["issues"].append(exc.message)
        return view
