"""Read-only TermiX client — the quant clients this agent is trading for.

WHY THIS EXISTS SEPARATELY FROM THE SKILL
=========================================

The `termix-bnb-agent-skills` package drives TermiX from an operator's laptop
with `WALLET_KEY` in the environment. This agent has no raw key: its key lives
in an encrypted keystore and is only reachable through ``get_wallet()``, which
exposes ``sign_message`` and nothing lower. So the four GETs the dashboard needs
are re-implemented here against the same wallet the agent signs everything else
with — the account TermiX knows is the account that owns the position.

READ-ONLY, DELIBERATELY
=======================

Nothing here creates a job, opens an envelope, or places a trade. A client
session envelope contains a private key that spends someone else's money; the
dashboard has no business decrypting one, and a bug in a rendering path must
never be able to. `trade` and `report` stay in the operator's hands, where a
human confirms each broadcast.

AUTH
====

EIP-191, no password: POST a nonce request, sign the exact `message` string the
server returns, and post the SIGNATURE BACK WITH THE NONCE — not the message.
The token is cached in memory for the process lifetime and re-minted on 401.
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Any

import httpx

log = logging.getLogger("lp-rebalancer.termix")

BASE_URL = os.environ.get("TERMIX_BASE_URL", "https://platform-backend-bnb8183.prod.termix.live")
AGENT_ID = os.environ.get("TERMIX_AGENT_ID", "")
TIMEOUT = 20.0

_lock = threading.Lock()
_token: str | None = None


def enabled() -> bool:
    """No agent id configured means this agent is not on TermiX. Not an error."""
    return bool(AGENT_ID)


def _login() -> str:
    """nonce -> sign -> session token.

    The nonce is single-use. The server wants the nonce echoed back next to the
    signature; posting the signed MESSAGE instead is the mistake this comment
    exists to prevent.
    """
    from bnbagent_studio_core.wallet import get_wallet

    wallet = get_wallet()
    address = str(wallet.address)
    with httpx.Client(base_url=BASE_URL, timeout=TIMEOUT) as client:
        res = client.post("/api/v1/auth/nonce", json={"walletAddress": address})
        res.raise_for_status()
        challenge = res.json()

        # sign_message returns the eth_account dict; `signature` is HexBytes.
        # str() on HexBytes gives "HexBytes('0x..')", so go through bytes() —
        # formatting it as text is how this ended up posting a signature the
        # server could only reject.
        signed = wallet.sign_message(challenge["message"])
        raw = signed["signature"] if isinstance(signed, dict) else signed
        sig = raw if isinstance(raw, str) else "0x" + bytes(raw).hex()
        if not sig.startswith("0x"):
            sig = "0x" + sig

        res = client.post("/api/v1/auth/wallet", json={
            "walletAddress": address,
            "nonce": challenge["nonce"],
            "signature": sig,
        })
        res.raise_for_status()
        body = res.json()
    token = body.get("accessToken") or body.get("token") or (body.get("data") or {}).get("accessToken")
    if not token:
        raise RuntimeError(f"TermiX login returned no token: {sorted(body)}")
    return token


def _get(path: str) -> Any:
    """One authenticated GET, re-minting the session once on 401."""
    global _token
    for attempt in (1, 2):
        with _lock:
            if _token is None:
                _token = _login()
            token = _token
        with httpx.Client(base_url=BASE_URL, timeout=TIMEOUT) as client:
            res = client.get(path, headers={"authorization": f"Bearer {token}",
                                            "accept": "application/json"})
        if res.status_code == 401 and attempt == 1:
            with _lock:
                _token = None          # expired; one clean retry, never a loop
            continue
        res.raise_for_status()
        return res.json()
    raise RuntimeError("TermiX rejected a freshly minted session")


def _items(payload: Any) -> list[dict[str, Any]]:
    """The API returns either a bare list or {items:[...]} depending on route."""
    if isinstance(payload, dict):
        return payload.get("items") or payload.get("data") or []
    return payload if isinstance(payload, list) else []


def clients() -> dict[str, Any]:
    """Every strategy this agent lists, and every client session behind it.

    Trades come from the platform's INDEXER, reconstructed from the task
    wallet's own on-chain swaps — not from anything this agent reports. That is
    the whole point of the vertical, so the dashboard shows the indexer's
    numbers even where the agent could compute its own.
    """
    if not enabled():
        return {"enabled": False, "reason": "TERMIX_AGENT_ID is not set"}

    strategies = [s for s in _items(_get("/api/v1/quant/my-strategies"))
                  if not AGENT_ID or s.get("providerAgentId") == AGENT_ID]

    jobs: list[dict[str, Any]] = []
    for row in _items(_get(f"/api/v1/quant/inbox?agentId={AGENT_ID}")):
        job_id = row.get("quantJobId")
        if not job_id:
            continue
        try:
            job = dict(_get(f"/api/v1/quant/jobs/{job_id}"))
        except Exception as e:  # noqa: BLE001 — one unreadable job must not hide the rest
            jobs.append({"quantJobId": job_id, "error": f"{type(e).__name__}: {e}"})
            continue
        # The inbox row names it `envelopeId`; the raw API row calls it `id`.
        # Without the fallback this silently overwrote a real id with null.
        job["envelopeId"] = row.get("envelopeId") or row.get("id")
        job["envelopeAt"] = row.get("createdAt")
        try:
            job["trades"] = _items(_get(f"/api/v1/quant/jobs/{job_id}/trades"))
        except Exception:  # noqa: BLE001 — an empty trade list is the common case
            job["trades"] = []
        jobs.append(job)

    return {
        "enabled": True,
        "agent_id": AGENT_ID,
        "strategies": strategies,
        "jobs": jobs,
        # Principal never reaches this agent: it sits in a wallet only the
        # client can empty, and the agent holds a capped, expiring session key
        # over it. Stated here so the dashboard cannot imply custody.
        "custody": "none — client-owned task wallets, session key only",
    }
