"""Address resolution and the delivery-zone rule for the single restaurant.

The model structures what the customer said. This code finds places, grades them, and decides the
zone. Coordinates are saved only after the customer confirms one of the places found here.
"""
import math
import re
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, replace

from app.errors import DomainError, Unavailable
from app.areas import AreaRule, load_area_rules, match_area
from app.branches import Branch, load_branches
from app.config import Settings
from app.geo import GeoCandidate, GeoClient
from app.logging_setup import logger
from app.orders import OrderService

SEARCH_RADIUS_M = 12_000  # look around the restaurant, wider than the zone so "too far" can be explained
BOUNDARY_MARGIN_M = 250  # an imprecise pin this close to the edge goes to staff instead of being guessed
# Two origins this close are the same doorway at a 3 km radius. Beyond it, one of the two files is
# out of date and the zone is being measured from the wrong place.
MISMATCH_TOLERANCE_M = 100
SAME_PLACE_M = 150  # two matches this close are one place for the customer
MAX_QUERIES = 4
MAX_IN_FLIGHT = 2
USABLE = ("exact", "street")
PRECISION_ORDER = {"exact": 0, "street": 1, "area": 2, "too_coarse": 3}


def _words(text: str) -> set[str]:
    return {"phase" if w == "ph" else w for w in re.findall(r"[a-z0-9]+", text.lower())}


def relevance(fields: "AddressFields", label: str) -> int:
    """How many of the customer's own words (house, street, block, area) appear in a match. Real Lahore data
    ranks '12 Street 6' above '12 Street 5, Sec FF Ph 4' by confidence alone, so confidence is only a tiebreak."""
    wanted = _words(" ".join([fields.house, fields.street, fields.block, fields.area])) - {"dha", "lahore", "street"}
    return len(wanted & _words(label))


def haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    (lat1, lon1), (lat2, lon2) = a, b
    p1, p2 = math.radians(lat1), math.radians(lat2)
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * 6_371_000 * math.asin(math.sqrt(h))


@dataclass(frozen=True)
class Origin:
    """The one point every delivery distance is measured from, plus where it came from."""
    lat: float
    lon: float
    label: str
    source: str
    radius_m: int
    # How far this point sits from the restaurant pin in the environment file. None when there is no
    # pin to compare against, or when the pin itself is what supplied the coordinates.
    mismatch_m: int | None = None


def resolve_origin(branch: Branch | None, configured_lat: float | None, configured_lon: float | None,
                   default_radius_m: int, branches_source: str = "data/branches.json") -> Origin | None:
    """Decide where delivery is measured from, and keep the audit trail for that decision.

    A branch record carrying its own coordinates wins, because the radius belongs to the branch. The
    environment pin is then only a second opinion, and the gap between the two is reported so a stale
    file cannot move the delivery zone without anyone noticing. With no branch coordinates the pin is
    used directly, which is what makes editing the environment file do something.
    """
    has_pin = configured_lat is not None and configured_lon is not None
    pin = (float(configured_lat), float(configured_lon)) if has_pin else None

    if branch is not None and branch.lat is not None and branch.lon is not None:
        point = (branch.lat, branch.lon)
        label = f"{branch.name} ({branch.id})"
        if branch.from_fallback:
            return Origin(point[0], point[1], label, "restaurant pin from the environment",
                          branch.radius_m, None)
        mismatch = round(haversine_m(point, pin)) if pin is not None else None
        return Origin(point[0], point[1], label, branches_source, branch.radius_m, mismatch)

    if pin is not None:
        return Origin(pin[0], pin[1], "restaurant pin", "restaurant pin from the environment",
                      default_radius_m, None)
    return None


@dataclass(frozen=True)
class AddressFields:
    raw: str
    house: str = ""
    street: str = ""
    block: str = ""  # block, sector or society block
    area: str = ""  # phase, society, neighbourhood
    landmark: str = ""
    city: str = "Lahore"


class DeliveryService:
    def __init__(self, orders: OrderService, geo: GeoClient | None, lat: float | None, lon: float | None,
                 radius_m: int = 3000, pin_verified: bool = False, hedge_s: float = 1.5, hard_s: float = 8.0,
                 area_rules: list[AreaRule] | None = None):
        # Geocoding is the slow step. Measured against Geoapify: 1 to 11 s for the same request, and several
        # requests at once make each of them slower (6 at once: up to 9 s), so sending more is not faster.
        # Send one query. Start a second only if the first is slow (hedge_s) or gave nothing usable, never more
        # than MAX_IN_FLIGHT at once, and give up at hard_s.
        self.hedge_s, self.hard_s = hedge_s, hard_s
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="geo")
        self.orders = orders
        self.geo = geo
        self.origin = (lat, lon) if lat is not None and lon is not None else None
        self.radius_m = radius_m
        self.pin_verified = pin_verified
        self.area_rules = area_rules or []
        self.branch: Branch | None = None

    def set_branch(self, branch: Branch) -> None:
        if branch.lat is None or branch.lon is None:
            raise ValueError(f"branch {branch.id} has no resolved coordinates")
        self.branch = branch
        self.origin = (branch.lat, branch.lon)
        self.radius_m = branch.radius_m

    def set_origin(self, origin: Origin) -> None:
        """Point delivery at the one resolved origin. Called once at startup, before any request."""
        self.origin = (origin.lat, origin.lon)
        self.radius_m = origin.radius_m

    def check_area(self, text: str) -> dict:
        """Check an area without requiring a house number; Geoapify's cache handles repeat questions."""
        rule = match_area(text, self.area_rules)
        if rule is not None:
            return {"status": rule.outcome, "area": rule.name, "message": rule.message}
        geo, origin = self._require_ready()
        query = text if "lahore" in text.lower() else f"{text}, Lahore"
        try:
            candidates = geo.geocode(query, origin, SEARCH_RADIUS_M, limit=3)
        except Unavailable:
            return {"status": "unavailable", "message": "I cannot check that area right now; please try again."}
        usable = [c for c in candidates if c.precision in ("area", "street", "exact")]
        if not usable:
            return {"status": "need_address", "message": "I need the full address to check that area."}
        candidate = min(usable, key=lambda c: haversine_m(origin, (c.lat, c.lon)))
        distance = round(haversine_m(origin, (candidate.lat, candidate.lon)))
        if distance > self.radius_m + BOUNDARY_MARGIN_M:
            return {"status": "outside", "area": candidate.label,
                    "message": f"Sorry, {candidate.label} is outside our {self.radius_m / 1000:g} km delivery area."}
        if distance <= self.radius_m - BOUNDARY_MARGIN_M:
            return {"status": "within", "area": candidate.label,
                    "message": f"We generally deliver in {candidate.label}. A full address is only needed to place the order."}
        return {"status": "need_address", "area": candidate.label,
                "message": "That area is near our delivery boundary, so I need the full address to confirm it."}

    def _require_ready(self) -> tuple[GeoClient, tuple[float, float]]:
        if self.geo is None or self.origin is None:
            raise Unavailable("delivery_not_configured", "delivery checks are not set up yet, staff must take this order")
        return self.geo, self.origin

    # ---- address resolution ------------------------------------------------

    def _queries(self, f: AddressFields) -> list[str]:
        """Best first. The house-less query answers fastest upstream and street level is enough to decide a
        3 km zone, so it goes first and the one with the house number is only a fallback."""
        join = lambda *p: ", ".join(x.strip() for x in p if x and x.strip())
        street = f"Street {f.street.strip()}" if f.street.strip().isdigit() else f.street
        specific = join(f.house, street, f.block, f.area, f.city)
        house_less = join(street, f.block, f.area, f.city)
        landmark = join(f.landmark, f.area, f.city) if f.landmark else ""
        raw = f.raw if "lahore" in f.raw.lower() else join(f.raw, "Lahore")
        has_place_detail = bool(f.house.strip() or street.strip() or f.block.strip())
        if has_place_detail:
            ordered = [house_less, specific, landmark, raw]
        else:
            ordered = [landmark, specific, raw]  # "Phase 5, Lahore" alone is too vague to lead with
        return list(dict.fromkeys(q for q in ordered if q))[:MAX_QUERIES]

    def _search(self, fields: AddressFields) -> list[GeoCandidate]:
        geo, origin = self._require_ready()
        queries = self._queries(fields)
        for attempt in (1, 2):
            try:
                found = self._race(geo, origin, queries)
                break
            except Unavailable as exc:
                # A quick failure (DNS blip, 5xx) is worth one more round. A timeout already spent its time.
                if attempt == 2 or exc.code != "geo_unavailable" or getattr(exc, "no_retry", False):
                    raise
                logger.info("address_search_retry")
        ranked = sorted(found, key=lambda c: (PRECISION_ORDER[c.precision], -relevance(fields, c.label),
                                              -c.confidence, haversine_m(origin, (c.lat, c.lon))))
        kept: list[GeoCandidate] = []
        for c in ranked:
            if not any(haversine_m((c.lat, c.lon), (k.lat, k.lon)) < SAME_PLACE_M for k in kept):
                kept.append(c)
        return kept

    def _race(self, geo: GeoClient, origin: tuple[float, float], queries: list[str]) -> list[GeoCandidate]:
        waiting = list(queries)
        in_flight: set = set()
        found: list[GeoCandidate] = []
        errors: list[Unavailable] = []
        started = last_launch = time.monotonic()
        sent = 0

        def launch() -> None:
            nonlocal last_launch, sent
            in_flight.add(self._pool.submit(geo.geocode, waiting.pop(0), origin, SEARCH_RADIUS_M))
            last_launch, sent = time.monotonic(), sent + 1

        launch()
        while in_flight:
            now = time.monotonic()
            remaining = self.hard_s - (now - started)
            if remaining <= 0:
                break  # anything still running keeps going in the background and still fills the cache
            can_hedge = bool(waiting) and len(in_flight) < MAX_IN_FLIGHT
            timeout = min(remaining, max(self.hedge_s - (now - last_launch), 0.0)) if can_hedge else remaining
            done, in_flight = wait(in_flight, timeout=timeout, return_when=FIRST_COMPLETED)
            for future in done:
                try:
                    found += future.result()
                except Unavailable as exc:
                    errors.append(exc)
            if any(c.precision in USABLE for c in found):
                break
            if waiting and len(in_flight) < MAX_IN_FLIGHT and (done or time.monotonic() - last_launch >= self.hedge_s):
                launch()
        logger.info("address_search", extra={"sent": sent, "ms": round((time.monotonic() - started) * 1000),
                                             "results": len(found), "failed": len(errors),
                                             "left_running": len(in_flight)})
        if not found:
            if errors:
                raise errors[0]
            if in_flight or waiting:
                raise Unavailable("geo_timeout", "the map service is too slow right now")
        return found

    def resolve(self, order_id: int, fields: AddressFields) -> dict:
        """Find places for what the customer said. Returns what the model should do next."""
        _, origin = self._require_ready()
        if not (fields.house.strip() or fields.street.strip() or fields.block.strip() or fields.landmark.strip()):
            self.orders.begin_address(order_id, fields.raw, [])
            return {"status": "need_detail", "ask": "Please provide your address, including a house, street, block, or landmark. I will not guess it."}
        if self.branch is not None:
            self.orders.set_branch(order_id, self.branch.id, self.branch.name, self.branch.radius_m)
        candidates = self._search(fields)
        usable = [c for c in candidates if c.precision in ("exact", "street")][:3]
        if not usable:
            self.orders.begin_address(order_id, fields.raw, [])
            status = "not_found" if not candidates else "need_detail"
            logger.info("address_unresolved", extra={"order_id": order_id, "status": status})
            return {"status": status,
                    "ask": "Ask for the block or sector, street name, house number, or a well known landmark nearby. "
                           "Do not guess."}
        self.orders.begin_address(order_id, fields.raw, [
            {"label": c.label, "lat": c.lat, "lon": c.lon, "place_id": c.place_id,
             "precision": c.precision, "confidence": c.confidence}
            for c in usable])
        shown = [{"number": i + 1, "label": c.label, "place_id": c.place_id,
                  "confidence": c.confidence, "looks_outside_zone":
                  haversine_m(origin, (c.lat, c.lon)) > self.radius_m + BOUNDARY_MARGIN_M}
                 for i, c in enumerate(usable)]
        logger.info("address_candidates", extra={"order_id": order_id, "count": len(usable)})
        if len(usable) == 1 and usable[0].precision == "exact" and usable[0].confidence < 0.5:
            return {"status": "need_detail", "candidates": shown,
                    "ask": "The map result is not confident enough. Ask the customer to clarify the house, street, block, or landmark."}
        if len(usable) == 1:
            return {"status": "confirm", "candidates": shown,
                    "ask": "Read this place back and ask the customer if it is right. Call confirm_address(1) only "
                           "after they say yes in their next message."}
        return {"status": "choose", "candidates": shown,
                "ask": "Several places match. Read them out by number and ask which one is the customer's. Call "
                       "confirm_address with their number only after they answer in their next message."}

    def resolve_location(self, order_id: int, lat: float, lon: float) -> dict:
        geo, _ = self._require_ready()
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise DomainError("invalid_location", "the device location is invalid")
        candidate = geo.reverse_geocode(lat, lon)
        if candidate is None:
            self.orders.begin_address(order_id, "device location", [])
            return {"status": "not_found", "ask": "I could not read the device location. Please provide your address."}
        self.orders.begin_address(order_id, "device location", [{"label": candidate.label, "lat": candidate.lat,
            "lon": candidate.lon, "place_id": candidate.place_id, "precision": candidate.precision,
            "confidence": candidate.confidence}])
        return {"status": "confirm", "candidates": [{"number": 1, "label": candidate.label,
            "place_id": candidate.place_id, "confidence": candidate.confidence}],
            "ask": f"I found your location as {candidate.label}. Is that correct?"}

    # ---- delivery zone -----------------------------------------------------

    def check(self, order_id: int) -> dict:
        """Straight line first (a road is never shorter), then the road distance. Fails closed."""
        geo, origin = self._require_ready()
        target = self.orders.delivery_target(order_id)
        dest = (target["lat"], target["lon"])
        self.orders.begin_delivery_check(order_id)

        straight = round(haversine_m(origin, dest))
        if straight > self.radius_m + BOUNDARY_MARGIN_M:
            distance = straight  # the real road distance is at least this, no need to call the router
        else:
            distance = geo.route_distance_m(origin, dest)  # a map outage leaves the order in CHECKING_DELIVERY

        allowed = distance <= self.radius_m
        near_edge = abs(distance - self.radius_m) <= BOUNDARY_MARGIN_M and target["precision"] != "exact"
        pin_note = "" if self.pin_verified else " (restaurant pin unverified)"
        handoff = (f"{distance} m is within {BOUNDARY_MARGIN_M} m of the {self.radius_m} m limit and the address pin "
                   f"is only {target['precision']}-level, staff must confirm{pin_note}") if near_edge else None
        self.orders.finish_delivery(order_id, distance, allowed, handoff)
        logger.info("delivery_checked", extra={"order_id": order_id, "distance_m": distance, "allowed": allowed,
                                               "handoff": bool(handoff), "pin_verified": self.pin_verified})
        if handoff:
            return {"outcome": "handoff", "tell_customer": "That address is right at the edge of our delivery area, "
                    "so a team member will confirm it with you."}
        if allowed:
            return {"outcome": "ready", "tell_customer": "Good news, we deliver to that address."}
        return {"outcome": "out_of_zone", "tell_customer": f"Sorry, that address is outside our "
                f"{self.radius_m / 1000:g} km delivery area, so we cannot deliver this order."}


def configure_delivery(settings: Settings, orders: OrderService, geo: GeoClient | None) -> DeliveryService:
    """Build the delivery service and point it at the one origin, saying where that origin came from.

    The API and the terminal demo both call this. They used to carry a copy of the wiring each, which
    is how the branch file came to override the configured pin without anyone seeing it happen.
    """
    service = DeliveryService(orders, geo, None, None, settings.delivery_radius_m,
                              settings.restaurant_pin_verified,
                              area_rules=load_area_rules(settings.delivery_areas_path))
    branches = load_branches(settings.branches_path, settings.restaurant_lat, settings.restaurant_lon)
    branch = branches[0] if branches else None
    source = settings.branches_path

    if (branch is not None and not branch.from_fallback and branch.lat is None and geo is not None
            and settings.restaurant_lat is not None and settings.restaurant_lon is not None):
        # No pin configured, so the branch's own address is all there is. Ask the map for a point.
        matches = geo.geocode(branch.address, (settings.restaurant_lat, settings.restaurant_lon), 50_000, limit=1)
        if matches:
            branch = replace(branch, lat=matches[0].lat, lon=matches[0].lon)
            source = f"{settings.branches_path} (address geocoded)"

    if branch is not None and branch.lat is not None and branch.lon is not None:
        service.branch = branch

    origin = resolve_origin(branch, settings.restaurant_lat, settings.restaurant_lon,
                            settings.delivery_radius_m, source)
    if origin is None:
        logger.warning("delivery_origin_missing",
                       extra={"branches_path": settings.branches_path,
                              "has_pin": settings.restaurant_lat is not None
                              and settings.restaurant_lon is not None})
        return service

    service.set_origin(origin)
    logger.info("delivery_origin", extra={"label": origin.label, "source": origin.source, "lat": origin.lat,
                                          "lon": origin.lon, "radius_m": origin.radius_m})
    if origin.mismatch_m is not None and origin.mismatch_m > MISMATCH_TOLERANCE_M:
        logger.warning("delivery_origin_mismatch",
                       extra={"in_use": origin.source, "in_use_lat": origin.lat, "in_use_lon": origin.lon,
                              "config_lat": settings.restaurant_lat, "config_lon": settings.restaurant_lon,
                              "distance_m": origin.mismatch_m, "tolerance_m": MISMATCH_TOLERANCE_M})
    return service
