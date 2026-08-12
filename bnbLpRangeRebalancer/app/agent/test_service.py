"""Checks for the Service Layer REST surface (spec 8).

Read routes hit the live chain, so this is not a pure-offline suite — but the
two things it really guards are offline facts:

  1. Every spec 8 route EXISTS and is wired to the Agent Layer.
  2. The control routes cannot be called without the API key, and refuse
     entirely when no key is configured.

    python test_service.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parent.parent / "service"
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

from fastapi.testclient import TestClient  # noqa: E402

import api  # noqa: E402

# Spec 8 names these exactly. A route that disappears should fail loudly.
READ_ROUTES = ["/health", "/status", "/strategy", "/performance",
               "/positions", "/transactions", "/metadata"]
CONTROL_ROUTES = ["/activate", "/pause", "/execute"]

client = TestClient(api.app, raise_server_exceptions=False)


def test_every_spec8_route_is_registered():
    """Offline: the routes exist, whatever the chain is doing."""
    paths = {r.path for r in api.app.routes}
    missing = [p for p in READ_ROUTES + CONTROL_ROUTES if p not in paths]
    assert not missing, f"spec 8 routes not registered: {missing}"


def test_control_routes_refuse_without_a_configured_key():
    """Fail CLOSED. An open /execute is a rebalance any caller can trigger."""
    saved = os.environ.pop("SERVICE_API_KEY", None)
    try:
        for route in CONTROL_ROUTES:
            r = client.post(route)
            assert r.status_code == 503, (route, r.status_code, r.text)
            assert "disabled" in r.text, (route, r.text)
    finally:
        if saved is not None:
            os.environ["SERVICE_API_KEY"] = saved


def test_control_routes_reject_a_wrong_key():
    os.environ["SERVICE_API_KEY"] = "correct-horse"
    try:
        for route in CONTROL_ROUTES:
            assert client.post(route).status_code == 401, route
            assert client.post(route, headers={"X-API-Key": "wrong"}).status_code == 401, route
    finally:
        os.environ.pop("SERVICE_API_KEY", None)


def test_pause_is_reachable_with_the_right_key():
    """pause() is the one control route that is safe to actually run: it moves
    no funds, and leaving the agent paused is the safe resting state."""
    os.environ["SERVICE_API_KEY"] = "correct-horse"
    try:
        r = client.post("/pause", headers={"X-API-Key": "correct-horse"})
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "paused", r.text
    finally:
        os.environ.pop("SERVICE_API_KEY", None)


def test_metadata_is_self_describing():
    """Spec 9: the metadata document is what the marketplace reads."""
    body = client.get("/metadata").json()
    assert body["category"] == "rebalancing", body
    assert body["pair"] == "BNB/USDT", body
    assert "PancakeSwap V3" in body["protocols"], body
    # Every spec 4.7 action must be listed.
    for action in ("activate", "pause", "rebalance",
                   "getStatus", "getPosition", "getPerformance"):
        assert action in body["actions"], (action, body["actions"])
    for route in READ_ROUTES + CONTROL_ROUTES:
        assert any(route.lstrip("/") in str(v) for v in body["endpoints"].values()), route


def test_strategy_route_reports_configured_params_not_defaults():
    """Guards the cwd bug: the service dir has its own studio.toml with no
    [strategy] table, so a cwd-relative config load silently served DEFAULTS —
    a service reporting parameters the operator never set."""
    body = client.get("/strategy").json()
    params = body.get("parameters", {})
    assert params.get("token_id"), f"token_id fell back to a default: {params}"
    assert "mint_slippage_pct" in params, f"config not loaded from the agent: {params}"


def _live_read_routes():
    """Live: every read route answers 200 against the configured chain."""
    for route in READ_ROUTES:
        r = client.get(route)
        assert r.status_code == 200, (route, r.status_code, r.text[:200])
        print(f"  ok  GET {route}")
    print("live read routes OK")


def main() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"{len(tests)} passed")
    if "--live" in sys.argv:
        _live_read_routes()


if __name__ == "__main__":
    main()
