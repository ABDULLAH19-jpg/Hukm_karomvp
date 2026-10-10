"""Phase 3 hardening tests: shared-secret API auth and per-IP rate limiting."""
import logging

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.config import SecretStr, Settings
from app.db import make_session_factory
from app.delivery import DeliveryService
from app.orders import OrderService
from app.tools import build_tools
from tests.conftest import ORIGIN


def _settings(db_url, **overrides):
    base = dict(_env_file=None, database_url=db_url, menu_path="data/menu.json",
                restaurant_lat=ORIGIN[0], restaurant_lon=ORIGIN[1],
                branches_path="tests/data_branches.json")
    base.update(overrides)
    return Settings(**base)


# ---- API auth -------------------------------------------------------------


def test_no_auth_required_when_secret_unset(db_url):
    with TestClient(create_app(_settings(db_url), models=[], geo=None)) as c:
        resp = c.post("/orders", headers={"Idempotency-Key": "k-no-auth"})
        assert resp.status_code == 201


def test_auth_required_when_secret_set(db_url):
    settings = _settings(db_url, api_secret=SecretStr("sekrit"))
    with TestClient(create_app(settings, models=[], geo=None)) as c:
        # No header → 401
        resp = c.post("/orders", headers={"Idempotency-Key": "k1"})
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "unauthorized"

        # Wrong header → 401
        resp = c.post("/orders", headers={"Idempotency-Key": "k2", "Authorization": "Bearer nope"})
        assert resp.status_code == 401

        # Correct header → success
        resp = c.post("/orders", headers={"Idempotency-Key": "k3", "Authorization": "Bearer sekrit"})
        assert resp.status_code == 201


def test_public_endpoints_skip_auth(db_url):
    settings = _settings(db_url, api_secret=SecretStr("sekrit"))
    with TestClient(create_app(settings, models=[], geo=None)) as c:
        assert c.get("/health").status_code == 200
        assert c.get("/schedule").status_code == 200
        assert c.get("/menu/search", params={"q": "pizza"}).status_code == 200


def test_vapi_endpoint_uses_its_own_auth(db_url):
    # The Vapi webhook is not subject to api_secret; it has its own secret.
    settings = _settings(db_url, api_secret=SecretStr("api-sekrit"),
                         vapi_webhook_secret=SecretStr("vapi-sekrit"))
    with TestClient(create_app(settings, models=[], geo=None)) as c:
        # api_secret alone does NOT grant vapi access
        resp = c.post("/vapi/tool-calls", json={"message": {"type": "tool-calls"}},
                      headers={"Authorization": "Bearer api-sekrit"})
        assert resp.status_code == 401

        # vapi secret works
        resp = c.post("/vapi/tool-calls", json={"message": {"type": "tool-calls"}},
                      headers={"Authorization": "Bearer vapi-sekrit"})
        assert resp.status_code == 200


# ---- rate limiting --------------------------------------------------------


def test_rate_limiter_rejects_after_threshold(db_url):
    settings = _settings(db_url, rate_limit_per_minute=3)
    with TestClient(create_app(settings, models=[], geo=None)) as c:
        statuses = [c.post("/orders", headers={"Idempotency-Key": f"r{i}"}).status_code for i in range(5)]
        assert statuses[:3] == [201, 201, 201]
        assert statuses[3] == 429
        assert statuses[4] == 429


def test_rate_limiter_skips_get_requests(db_url):
    settings = _settings(db_url, rate_limit_per_minute=1)
    with TestClient(create_app(settings, models=[], geo=None)) as c:
        for _ in range(5):
            assert c.get("/health").status_code == 200


def test_rate_limiter_disabled_when_zero(db_url):
    settings = _settings(db_url, rate_limit_per_minute=0)
    with TestClient(create_app(settings, models=[], geo=None)) as c:
        statuses = [c.post("/orders", headers={"Idempotency-Key": f"z{i}"}).status_code for i in range(5)]
        assert statuses == [201, 201, 201, 201, 201]


# ---- PII redaction --------------------------------------------------------


def test_address_tool_args_are_redacted_in_logs(db_url, menu, geo, caplog):
    from app.errors import Unavailable

    service = OrderService(make_session_factory(db_url), menu)
    delivery = DeliveryService(service, geo, ORIGIN[0], ORIGIN[1], 3000, pin_verified=True)
    order, _ = service.create("pii-test")
    tools = {t.name: t for t in build_tools(service, order["id"], delivery=delivery)}

    secret_address = "House 12, Street 5, Secret Lane, DHA Phase 4"
    with caplog.at_level(logging.INFO, logger="hukm"):
        tools["resolve_address"].invoke({
            "raw": secret_address, "house": "12", "street": "5", "block": "FF",
            "area": "DHA Phase 4", "landmark": "Rafi Mosque",
        })

    logged = " ".join(r.getMessage() + " " + str(getattr(r, "tool_args", "")) for r in caplog.records)
    assert secret_address not in logged
    assert "Secret Lane" not in logged
    assert "<redacted>" in logged