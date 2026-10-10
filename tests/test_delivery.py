import logging
import time

import httpx
import pytest

from app.delivery import AddressFields, DeliveryService, haversine_m
from app.errors import Conflict, DomainError, Unavailable
from app.geo import GeoapifyClient
from app.db import make_session_factory
from app.orders import OrderService
from app.states import OrderState
from tests.conftest import ORIGIN, item_named, pizza_selections, place


@pytest.fixture
def service(menu, db_url):
    return OrderService(make_session_factory(db_url), menu)


@pytest.fixture
def delivery(service, delivery_for):
    return delivery_for(service)


@pytest.fixture
def oid(service, menu):
    order, _ = service.create("delivery-test")
    service.add_item(order["id"], item_named(menu, "Chicken Supreme").id, 1, pizza_selections())
    service.transition(order["id"], OrderState.VALIDATING_ORDER)
    return order["id"]


def say(raw="", **kw):
    return AddressFields(raw=raw or "my address", **kw)


def confirmed(delivery, service, oid, **fields):
    delivery.resolve(oid, say(**fields))
    service.confirm_address(oid, 1)


def code(fn, *args):
    with pytest.raises(DomainError) as exc:
        fn(*args)
    return exc.value.code


# ---- resolving what the customer said ---------------------------------------

def test_one_clear_match_is_offered_for_confirmation_not_saved(delivery, service, oid):
    out = delivery.resolve(oid, say(house="12", street="5", area="DHA Phase 4"))
    assert out["status"] == "confirm" and out["candidates"][0]["number"] == 1
    order = service.get(oid)
    assert order["state"] == "COLLECTING_ADDRESS" and order["address_confirmed"] is False


def test_structured_fields_become_the_search_query(delivery, geo, oid):
    geo.places = {}  # nothing matches, so every variant is sent and we can read them all
    delivery.resolve(oid, say(house="223", block="FF", area="DHA Phase 4"))
    assert geo.queries[:2] == ["FF, DHA Phase 4, Lahore", "223, FF, DHA Phase 4, Lahore"]


def test_falls_back_to_the_raw_words_when_fields_find_nothing(delivery, geo, oid):
    out = delivery.resolve(oid, AddressFields(raw="phase 4 ki wo gali", house="999", area="Defence"))
    assert out["status"] == "confirm"
    assert any(q.endswith("Lahore") and "wo gali" in q for q in geo.queries)


def test_customer_confirmation_saves_the_exact_coordinates(delivery, service, oid):
    delivery.resolve(oid, say(house="12", area="DHA Phase 4"))
    order = service.confirm_address(oid, 1)
    assert order["address_confirmed"] and order["address"].startswith("House 12")
    target = service.delivery_target(oid)
    assert (round(target["lat"], 4), round(target["lon"], 4)) == (round(ORIGIN[0] + 1200 / 111_000, 4),
                                                                  round(ORIGIN[1] + 500 / 95_000, 4))


def test_only_places_the_server_found_can_be_confirmed(delivery, service, oid):
    assert code(service.confirm_address, oid, 1) == "no_address_to_confirm"
    delivery.resolve(oid, say(house="12", area="DHA Phase 4"))
    assert code(service.confirm_address, oid, 2) == "unknown_candidate"
    assert code(service.confirm_address, oid, 0) == "unknown_candidate"


def test_city_level_match_is_never_offered(delivery, service, oid):
    out = delivery.resolve(oid, say(area="somewhere near lahore"))
    assert out["status"] == "need_detail" and "candidates" not in out
    assert code(service.confirm_address, oid, 1) == "no_address_to_confirm"


def test_area_level_match_asks_for_more_detail(delivery, oid):
    assert delivery.resolve(oid, say(area="DHA Lahore"))["status"] == "need_detail"


def test_nothing_found(delivery, oid):
    assert delivery.resolve(oid, say(house="999", area="Mars colony"))["status"] == "not_found"


def test_several_matches_ask_the_customer_to_choose(delivery, oid):
    out = delivery.resolve(oid, say(landmark="masjid"))
    assert out["status"] == "choose" and [c["number"] for c in out["candidates"]] == [1, 2]
    assert [c["looks_outside_zone"] for c in out["candidates"]] == [False, True]


def test_matches_that_share_the_customers_words_rank_first_even_with_lower_confidence(delivery, geo, oid):
    geo.places["ranking"] = [
        place("12 Street 6, Urban Lahore", north_m=300, confidence=0.63),
        place("C 12 Street 1, Defense Housing Authority", north_m=900, confidence=0.63),
        place("12 Street 5, Sec FF Ph 4, Defense", north_m=1500, confidence=0.45)]
    out = delivery.resolve(oid, say(house="12", street="5", block="FF", area="ranking"))
    assert out["candidates"][0]["label"] == "12 Street 5, Sec FF Ph 4, Defense"


def test_a_bare_street_number_is_searched_as_street_n(delivery, geo, oid):
    geo.places = {}
    delivery.resolve(oid, say(house="12", street="5", block="FF", area="DHA Phase 4"))
    assert "12, Street 5, FF, DHA Phase 4, Lahore" in geo.queries and "Street 5, FF, DHA Phase 4, Lahore" in geo.queries


def test_a_wrong_match_does_not_end_the_order(delivery, service, oid):
    confirmed(delivery, service, oid, area="phase 8", street="9")
    assert delivery.check(oid)["outcome"] == "out_of_zone"
    delivery.resolve(oid, say(house="12", area="DHA Phase 4"))  # the customer gives a different address
    service.confirm_address(oid, 1)
    assert delivery.check(oid)["outcome"] == "ready"
    assert service.get(oid)["state"] == "READY_FOR_CONFIRMATION"


def test_the_same_place_returned_twice_is_one_candidate(delivery, geo, oid):
    geo.places["twin"] = [place("Plot 1, DHA", north_m=500), place("DHA Plot 1", north_m=530, confidence=0.5)]
    assert len(delivery.resolve(oid, say(house="1", area="twin"))["candidates"]) == 1


def test_a_new_lookup_discards_the_old_confirmation(delivery, service, oid):
    confirmed(delivery, service, oid, house="12", area="DHA Phase 4")
    delivery.resolve(oid, say(area="Mars colony"))
    assert service.get(oid)["address_confirmed"] is False
    assert code(service.delivery_target, oid) == "address_not_confirmed"


# ---- the 3 km rule ------------------------------------------------------------

def test_address_inside_the_zone_is_approved(delivery, service, geo, oid):
    confirmed(delivery, service, oid, house="12", area="DHA Phase 4")
    out = delivery.check(oid)
    order = service.get(oid)
    assert out["outcome"] == "ready" and order["state"] == "READY_FOR_CONFIRMATION"
    assert order["distance_m"] == round(haversine_m(ORIGIN, (service.delivery_target(oid)["lat"],
                                                              service.delivery_target(oid)["lon"])) * 1.3)
    assert geo.route_calls == 1


def test_address_far_away_is_rejected_without_calling_the_router(delivery, service, geo, oid):
    confirmed(delivery, service, oid, area="phase 8", street="9")
    out = delivery.check(oid)
    assert out["outcome"] == "out_of_zone" and "3 km" in out["tell_customer"]
    assert service.get(oid)["state"] == "OUT_OF_ZONE" and geo.route_calls == 0


def test_short_straight_line_does_not_mean_inside_the_zone(delivery, service, geo, oid):
    geo.places["river"] = [place("Across the canal, DHA", north_m=2500)]
    geo.route_m = 4200  # the only road is a long detour
    confirmed(delivery, service, oid, house="1", area="river")
    assert delivery.check(oid)["outcome"] == "out_of_zone"
    assert service.get(oid)["distance_m"] == 4200


def test_imprecise_pin_near_the_edge_goes_to_staff(delivery, service, geo, oid):
    geo.places["edge"] = [place("Street 20, DHA", north_m=2400, precision="street")]
    geo.route_m = 2900
    confirmed(delivery, service, oid, house="1", area="edge")
    assert delivery.check(oid)["outcome"] == "handoff"
    assert service.get(oid)["state"] == "HANDOFF_REQUIRED"


def test_exact_pin_near_the_edge_is_decided_normally(delivery, service, geo, oid):
    geo.places["edge"] = [place("House 7, Street 20, DHA", north_m=2400, precision="exact")]
    geo.route_m = 2900
    confirmed(delivery, service, oid, house="1", area="edge")
    assert delivery.check(oid)["outcome"] == "ready"


def test_map_outage_never_approves_and_can_be_retried(delivery, service, geo, oid):
    confirmed(delivery, service, oid, house="12", area="DHA Phase 4")
    geo.route_error = True
    with pytest.raises(Unavailable):
        delivery.check(oid)
    order = service.get(oid)
    assert order["state"] == "CHECKING_DELIVERY" and order["distance_m"] is None
    geo.route_error = False
    assert delivery.check(oid)["outcome"] == "ready"


def test_geocoding_outage_changes_nothing(delivery, service, geo, oid):
    def boom(*a, **k):
        raise Unavailable("geo_timeout", "slow")
    geo.geocode = boom
    with pytest.raises(Unavailable):
        delivery.resolve(oid, say(house="12", area="DHA Phase 4"))
    assert service.get(oid)["state"] == "VALIDATING_ORDER"


def test_unconfigured_delivery_fails_closed(service, oid):
    nothing = DeliveryService(service, None, None, None)
    for call in (lambda: nothing.resolve(oid, say()), lambda: nothing.check(oid)):
        with pytest.raises(Unavailable) as exc:
            call()
        assert exc.value.code == "delivery_not_configured"
    assert service.get(oid)["state"] == "VALIDATING_ORDER"


def test_missing_pin_alone_also_fails_closed(service, geo, oid):
    with pytest.raises(Unavailable):
        DeliveryService(service, geo, None, None).resolve(oid, say())


# ---- the state machine enforces the zone, whoever calls it -----------------------

def test_no_caller_can_skip_the_zone_check(delivery, service, oid):
    confirmed(delivery, service, oid, house="12", area="DHA Phase 4")
    service.begin_delivery_check(oid)
    assert code(service.transition, oid, OrderState.READY_FOR_CONFIRMATION) == "delivery_not_approved"
    assert code(service.transition, oid, OrderState.OUT_OF_ZONE) == "delivery_not_rejected"


def test_address_must_be_confirmed_before_the_zone_check(delivery, service, oid):
    delivery.resolve(oid, say(house="12", area="DHA Phase 4"))
    assert code(delivery.check, oid) == "address_not_confirmed"
    assert code(service.transition, oid, OrderState.CHECKING_DELIVERY) == "address_required"


def test_changing_the_address_after_approval_voids_it(delivery, service, geo, oid):
    confirmed(delivery, service, oid, house="12", area="DHA Phase 4")
    delivery.check(oid)
    delivery.resolve(oid, say(area="phase 8", street="9"))
    order = service.get(oid)
    assert order["state"] == "COLLECTING_ADDRESS" and order["address_confirmed"] is False
    assert order["distance_m"] is None
    assert code(service.transition, oid, OrderState.READY_FOR_CONFIRMATION) == "invalid_transition"


def test_haversine_is_right():
    assert round(haversine_m((0, 0), (1, 0)) / 1000) == 111
    assert haversine_m(ORIGIN, ORIGIN) == 0


# ---- Geoapify client: parsing, errors, and never leaking the key ---------------------

def client_with(handler):
    geo = GeoapifyClient("SECRET-KEY-123")
    geo._http = httpx.Client(transport=httpx.MockTransport(handler))
    return geo


def test_geocode_grades_matches_and_sends_the_search_area():
    seen = {}

    def handler(request):
        seen.update(request.url.params)
        return httpx.Response(200, json={"results": [
            {"formatted": "12 Street 5", "lat": 31.46, "lon": 74.38, "result_type": "building", "housenumber": "12",
             "rank": {"confidence": 1}},
            {"formatted": "Street 5", "lat": 31.46, "lon": 74.39, "result_type": "street", "rank": {"confidence": 0.5}},
            {"formatted": "DHA", "lat": 31.47, "lon": 74.39, "result_type": "suburb"},
            {"formatted": "Lahore", "lat": 31.5, "lon": 74.3, "result_type": "city", "rank": {"confidence": 0.2}},
            {"formatted": "5 Street 1", "lat": 31.46, "lon": 74.4, "result_type": "building", "housenumber": "5",
             "rank": {"confidence": 0}}]})

    found = client_with(handler).geocode("street 5", ORIGIN, 12000)
    # the last one is a house-number match with zero confidence, real Geoapify junk, so it is not offered
    assert [c.precision for c in found] == ["exact", "street", "area", "too_coarse", "area"]
    assert seen["filter"].startswith("circle:74.38764,31.46124,12000") and seen["bias"].startswith("proximity:")


def test_route_distance_is_parsed():
    geo = client_with(lambda r: httpx.Response(200, json={"features": [{"properties": {"distance": 2745.6}}]}))
    assert geo.route_distance_m(ORIGIN, (31.47, 74.39)) == 2746


def test_route_with_no_result_is_an_error_not_zero():
    with pytest.raises(Unavailable) as exc:
        client_with(lambda r: httpx.Response(200, json={"features": []})).route_distance_m(ORIGIN, (31.47, 74.39))
    assert exc.value.code == "route_not_found"


@pytest.mark.parametrize("handler,expected", [
    (lambda r: httpx.Response(500), "geo_unavailable"),
    (lambda r: httpx.Response(429), "geo_unavailable"),
    (lambda r: httpx.Response(200, content=b"not json"), "geo_unavailable"),
    (lambda r: (_ for _ in ()).throw(httpx.ReadTimeout("slow", request=r)), "geo_timeout"),
])
def test_provider_failures_become_unavailable(handler, expected, caplog):
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(Unavailable) as exc:
            client_with(handler).geocode("x", ORIGIN, 1000)
    assert exc.value.code == expected
    assert "SECRET-KEY-123" not in caplog.text and "SECRET-KEY-123" not in str(exc.value)


def test_one_slow_call_is_retried_once():
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(200, json={"features": [{"properties": {"distance": 1500}}]})

    assert client_with(handler).route_distance_m(ORIGIN, (31.47, 74.39)) == 1500 and len(calls) == 2


def test_a_second_failure_is_reported_and_a_bad_key_is_not_retried():
    calls = []
    slow = lambda r: (calls.append(1), (_ for _ in ()).throw(httpx.ReadTimeout("slow", request=r)))[1]
    with pytest.raises(Unavailable):
        client_with(slow).route_distance_m(ORIGIN, (31.47, 74.39))
    assert len(calls) == 2
    calls.clear()
    with pytest.raises(Unavailable):  # geocoding does not retry itself, the query race covers it
        client_with(slow).geocode("x", ORIGIN, 1000)
    assert len(calls) == 1
    calls.clear()
    with pytest.raises(Unavailable):
        client_with(lambda r: (calls.append(1), httpx.Response(401))[1]).geocode("x", ORIGIN, 1000)
    assert len(calls) == 1


# ---- speed: parallel queries, deadlines, cache, warm connection -------------------------------

FIELDS = dict(house="12", street="5", block="FF", area="DHA Phase 4", landmark="masjid")
FAST = "street 5, ff, dha phase 4, lahore"  # the house-less query, sent first
PRECISE = "12, street 5, ff, dha phase 4, lahore"  # with the house number, a fallback


def test_one_request_is_enough_when_the_first_answer_is_usable(delivery, geo, oid):
    out = delivery.resolve(oid, say(**FIELDS))
    assert out["status"] in ("confirm", "choose")
    assert [q.lower() for q in geo.queries] == [FAST]


def test_the_house_less_query_leads_and_the_house_number_one_is_a_fallback(delivery, geo, oid):
    geo.places = {}  # nothing matches, so every variant gets tried
    delivery.resolve(oid, say(**FIELDS))
    assert [q.lower() for q in geo.queries][:2] == [FAST, PRECISE]


def test_a_landmark_leads_when_there_is_no_street_block_or_house(delivery, geo, oid):
    geo.places = {}
    delivery.resolve(oid, say(area="Phase 5", landmark="masjid"))
    assert geo.queries[0] == "masjid, Phase 5, Lahore"


def test_a_slow_first_query_is_hedged_with_a_second_one(delivery, geo, oid):
    geo.places = {"12, street 5": [place("12 Street 5, Sec FF Ph 4", north_m=1000)],
                  "street 5, ff": [place("Street 5, Sec FF Ph 4", north_m=1100, precision="street")]}
    geo.delays = {FAST: 1.0}
    delivery.hedge_s = 0.1
    started = time.monotonic()
    out = delivery.resolve(oid, say(house="12", street="5", block="FF", area="DHA Phase 4"))
    assert time.monotonic() - started < 0.7
    assert [q.lower() for q in geo.queries] == [FAST, PRECISE]
    assert out["candidates"][0]["label"] == "12 Street 5, Sec FF Ph 4"


def test_an_unusable_first_answer_starts_the_next_query_without_waiting(delivery, geo, oid):
    geo.places = {"12, street 5": [place("12 Street 5, Sec FF Ph 4", north_m=1000)],  # first: the longer key wins
                  "street 5, ff": [place("Lahore", precision="too_coarse")]}
    delivery.hedge_s = 5.0  # a long hedge timer must not slow down a clean "nothing usable"
    started = time.monotonic()
    out = delivery.resolve(oid, say(house="12", street="5", block="FF", area="DHA Phase 4"))
    assert time.monotonic() - started < 0.5 and out["status"] == "confirm"


def test_a_failed_query_moves_straight_on_to_the_next(delivery, geo, oid):
    geo.fail_next = 1
    delivery.hedge_s = 5.0
    started = time.monotonic()
    out = delivery.resolve(oid, say(house="12", street="5", block="FF", area="DHA Phase 4"))
    assert time.monotonic() - started < 0.5 and out["status"] in ("confirm", "choose")


def test_never_more_than_two_requests_at_once(delivery, geo, oid):
    geo.places = {}
    geo.delays = {q.lower(): 0.25 for q in [
        "Street 5, FF, DHA Phase 4, Lahore", "12, Street 5, FF, DHA Phase 4, Lahore",
        "masjid, DHA Phase 4, Lahore", "12 street 5 phase 4, Lahore"]}
    delivery.hedge_s = 0.05
    delivery.resolve(oid, AddressFields(raw="12 street 5 phase 4", landmark="masjid", **{
        k: v for k, v in FIELDS.items() if k != "landmark"}))
    assert len(geo.queries) == 4 and geo.max_in_flight == 2


def test_no_answer_by_the_hard_deadline_is_a_timeout_and_changes_nothing(delivery, geo, service, oid):
    geo.delays = {FAST: 1.0, PRECISE: 1.0}
    delivery.hedge_s, delivery.hard_s = 0.05, 0.3
    started = time.monotonic()
    with pytest.raises(Unavailable) as exc:
        delivery.resolve(oid, say(house="12", street="5", block="FF", area="DHA Phase 4"))
    assert exc.value.code == "geo_timeout" and time.monotonic() - started < 0.8
    assert service.get(oid)["state"] == "VALIDATING_ORDER"


def test_a_quick_outage_is_retried_once_but_not_forever(delivery, geo, service, oid):
    geo.fail_next = 4  # every query of the first round fails
    assert delivery.resolve(oid, say(**FIELDS))["status"] in ("confirm", "choose")
    geo.fail_next = 100
    with pytest.raises(Unavailable) as exc:
        delivery.resolve(oid, say(**FIELDS))
    assert exc.value.code == "geo_unavailable"


def test_a_repeated_lookup_is_answered_from_the_cache():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(200, json={"results": [{"formatted": "A", "lat": 31.46, "lon": 74.38,
                                                      "result_type": "street", "rank": {"confidence": 0.5}}]})

    geo = client_with(handler)
    first = geo.geocode("Street 5, FF", ORIGIN, 12000)
    assert geo.geocode("  street 5,   ff ", ORIGIN, 12000) == first and len(calls) == 1
    geo.geocode("Street 6, FF", ORIGIN, 12000)
    assert len(calls) == 2


def test_failures_are_never_cached_and_old_entries_expire():
    now = [0.0]
    answers = [httpx.Response(500), httpx.Response(200, json={"results": []}), httpx.Response(200, json={"results": []})]
    calls = []

    def handler(request):
        calls.append(1)
        return answers[len(calls) - 1]

    geo = GeoapifyClient("SECRET-KEY-123", clock=lambda: now[0])
    geo._http = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(Unavailable):
        geo.geocode("x", ORIGIN, 1000)
    geo.geocode("x", ORIGIN, 1000)
    geo.geocode("x", ORIGIN, 1000)
    assert len(calls) == 2  # the failure was retried, the success was then served from the cache
    now[0] = 4000  # past the one hour lifetime
    answers.append(httpx.Response(200, json={"results": []}))
    geo.geocode("x", ORIGIN, 1000)
    assert len(calls) == 3


def test_warm_up_never_blocks_or_raises():
    geo = client_with(lambda r: httpx.Response(500))
    started = time.monotonic()
    thread = geo.warm_up()
    thread.join(timeout=2)
    assert time.monotonic() - started < 2 and not thread.is_alive()
