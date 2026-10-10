"""Service hours and last-order cutoff, in the restaurant's local time.

One restaurant, one daily schedule. Hours are compared in Asia/Karachi (UTC+5, no DST). The
server decides whether an order can be taken; the model only relays the reason to the caller.
"""
from dataclasses import dataclass
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

KARACHI = ZoneInfo("Asia/Karachi")


@dataclass(frozen=True)
class Schedule:
    opening: time
    last_order: time  # last moment a new order is accepted
    closing: time
    tz: ZoneInfo = KARACHI

    def status(self, now: datetime | None = None) -> tuple[str, str]:
        """The open/closed state and a short reason for the caller. ``open`` is the only state that
        accepts a new order.

        Handles both same-day (e.g. 11:00–23:00) and overnight (e.g. 11:00–03:00 next day)
        schedules. When closing <= opening the service crosses midnight."""
        if now is None:
            now = datetime.now(timezone.utc)
        local = now.astimezone(self.tz).timetz().replace(tzinfo=None)

        def _min(t: time) -> int:
            return t.hour * 60 + t.minute

        t = _min(local)
        op = _min(self.opening)
        lo = _min(self.last_order)
        cl = _min(self.closing)

        if cl <= op:
            # Overnight: service spans midnight. Map to a service clock where opening = 0.
            t_svc = (t - op) % 1440
            lo_svc = (lo - op) % 1440
            cl_svc = (cl - op) % 1440
            if t_svc < cl_svc:
                if t_svc <= lo_svc:
                    return "open", "We are open and taking orders."
                return "after_cutoff", (f"We have stopped taking orders for today; we close at "
                                        f"{self.closing:%H:%M}.")
            return "closed", f"We are closed now; we open at {self.opening:%H:%M}."
        else:
            if t < op:
                return "before_open", f"We open at {self.opening:%H:%M} today."
            if t <= lo:
                return "open", "We are open and taking orders."
            if t < cl:
                return "after_cutoff", (f"We have stopped taking orders for today; we close at "
                                        f"{self.closing:%H:%M}.")
            return "closed", f"We are closed now; we open at {self.opening:%H:%M} tomorrow."

    def can_take_orders(self, now: datetime | None = None) -> bool:
        return self.status(now)[0] == "open"


def parse_time(value: str) -> time:
    return datetime.strptime(value.strip(), "%H:%M").time()


def load_schedule(opening: str | None, last_order: str | None, closing: str | None) -> Schedule | None:
    """Return None when the restaurant has not configured hours, so callers can choose to run open."""
    if not (opening and last_order and closing):
        return None
    return Schedule(parse_time(opening), parse_time(last_order), parse_time(closing))
