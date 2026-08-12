"""Offline checks for the strategy layer's non-chain logic.

Every test here is pure: no RPC, no wallet, no transaction. They cover the
accounting and safety logic that a live run would only expose after it had
already moved money — which is exactly how the bugs these guard against were
found in the first place.

    python test_strategy.py
"""
from __future__ import annotations

import multiprocessing
import tempfile
import time
from pathlib import Path

import strategy as s


# --- fees_24h must not count a BNB price move as fee income --------------------
def test_fees_since_is_price_neutral():
    """A pure price move earns nothing, so the window must report nothing.

    The original code stored one combined USDT value per snapshot and
    differenced it, which booked the revaluation of the whole historical BNB
    fee balance as fees earned in the window.
    """
    day_ago = time.time() - 90000
    snaps = [{"ts": day_ago, "fees_usdt": 0.0, "fees_bnb": 0.01}]
    # Same fee balance, BNB up 10%: 0.01 BNB revalues by $0.60 and none of it
    # is income.
    earned, complete = s._fees_since(snaps, 86400, 0.0, 0.01, price=660.0)
    assert earned == 0.0, f"price move leaked into fees_24h: {earned}"
    assert complete is True


def test_fees_since_counts_real_fees():
    day_ago = time.time() - 90000
    snaps = [{"ts": day_ago, "fees_usdt": 1.0, "fees_bnb": 0.01}]
    # +$2 USDT and +0.01 BNB of genuine fees, valued at the current price.
    earned, complete = s._fees_since(snaps, 86400, 3.0, 0.02, price=600.0)
    assert abs(earned - 8.0) < 1e-9, earned
    assert complete is True


def test_fees_since_window_incomplete():
    """Watching for 10 minutes cannot report a 24h total."""
    snaps = [{"ts": time.time() - 600, "fees_usdt": 0.0, "fees_bnb": 0.0}]
    _, complete = s._fees_since(snaps, 86400, 1.0, 0.0, price=600.0)
    assert complete is False


def test_fees_since_skips_legacy_snapshots():
    """Pre-two-sided snapshots are dropped, not silently misread."""
    snaps = [{"ts": time.time() - 90000, "fees_usdt": 5.0}]  # no fees_bnb
    earned, complete = s._fees_since(snaps, 86400, 1.0, 0.0, price=600.0)
    assert (earned, complete) == (0.0, False)


def test_record_snapshot_keeps_both_sides():
    snaps = s._record_snapshot({}, 1.5, 0.02, 100.0, 600.0)
    assert snaps[-1]["fees_usdt"] == 1.5 and snaps[-1]["fees_bnb"] == 0.02


def test_record_snapshot_rate_limited():
    state = {"snapshots": [{"ts": time.time(), "fees_usdt": 0.0, "fees_bnb": 0.0}]}
    assert len(s._record_snapshot(state, 1.0, 0.0, 1.0, 600.0)) == 1


# --- studio.toml rewriting must stay inside [strategy] -------------------------
def test_persist_token_id_is_section_scoped():
    """A token_id in an earlier table must survive untouched."""
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "studio.toml"
        path.write_text(
            "[erc8004]\n"
            "token_id = 42\n"
            "\n"
            "[strategy]\n"
            "token_id = 7116214  # mainnet position\n"
        )
        s._persist_token_id(7200000, path)
        out = path.read_text()
        assert "token_id = 42" in out, f"clobbered an unrelated table:\n{out}"
        assert "token_id = 7200000" in out, out
        assert "# mainnet position" in out, "dropped the trailing comment"
        assert "7116214" not in out, out


def test_persist_token_id_survives_a_missing_file():
    """Best-effort by design: current_token_id reads the state file."""
    s._persist_token_id(1, Path("/nonexistent/dir/studio.toml"))  # must not raise


# --- one rebalance at a time, across processes --------------------------------
def _hold_lock(started, release):
    import strategy as st

    with st._rebalance_lock():
        started.set()
        release.wait(10)


def test_rebalance_lock_excludes_a_second_process():
    """The operator CLI and the server's monitor thread are different processes,
    so a threading.Lock would not have stopped the second one."""
    ctx = multiprocessing.get_context("fork")
    started, release = ctx.Event(), ctx.Event()
    p = ctx.Process(target=_hold_lock, args=(started, release))
    p.start()
    try:
        assert started.wait(10), "child never took the lock"
        try:
            with s._rebalance_lock():
                raise AssertionError("acquired a lock the child already holds")
        except RuntimeError as e:
            assert "already in progress" in str(e), e
    finally:
        release.set()
        p.join(10)

    # ...and it is released afterwards.
    with s._rebalance_lock():
        pass


def main() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"{len(tests)} passed")


if __name__ == "__main__":
    main()
