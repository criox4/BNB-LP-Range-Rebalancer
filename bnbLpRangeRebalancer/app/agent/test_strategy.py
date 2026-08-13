"""Offline checks for the strategy layer's non-chain logic.

Every test here is pure: no RPC, no wallet, no transaction. They cover the
accounting and safety logic that a live run would only expose after it had
already moved money — which is exactly how the bugs these guard against were
found in the first place.

    python test_strategy.py
"""
from __future__ import annotations

import multiprocessing
import json
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


def _isolated_store(tmp: Path):
    """Point strategy at a throwaway SQLite store (never the real one)."""
    import state_store
    s.DB_PATH = tmp / "lp_state.test.db"
    s.STATE_PATH = tmp / ".lp_state.test.json"
    s._store.cache_clear()
    return s._store()


def test_record_snapshot_keeps_both_sides():
    with tempfile.TemporaryDirectory() as d:
        _isolated_store(Path(d))
        s._record_snapshot(1.5, 0.02, 100.0, 600.0)
        snaps = s.load_state()["snapshots"]
        assert snaps[-1]["fees_usdt"] == 1.5 and snaps[-1]["fees_bnb"] == 0.02


def test_record_snapshot_rate_limited():
    """Two samples inside the gap must record ONE row, not two."""
    with tempfile.TemporaryDirectory() as d:
        _isolated_store(Path(d))
        s._record_snapshot(1.0, 0.0, 1.0, 600.0)
        s._record_snapshot(2.0, 0.0, 1.0, 600.0)
        assert len(s.load_state()["snapshots"]) == 1


def test_state_writes_are_atomic_and_appends_are_o1():
    """The two faults that motivated leaving JSON behind.

    A truncated write used to read as "start fresh", which silently reset
    token_id to the studio.toml bootstrap (B10). And history/snapshots were
    rewritten in full on a 60s path.
    """
    with tempfile.TemporaryDirectory() as d:
        store = _isolated_store(Path(d))
        s._update(status="active", token_id=7116214)
        for i in range(5):
            store.append_history({"at": f"t{i}", "action": "rebalance"})
        state = s.load_state()
        assert state["status"] == "active" and state["token_id"] == 7116214
        assert [h["at"] for h in state["history"]] == [f"t{i}" for i in range(5)]

        # update() must refuse a whole-list write — that is the regression the
        # append tables exist to prevent.
        try:
            store.update(history=[{"at": "clobber"}])
            raise AssertionError("update() accepted a whole-list history write")
        except TypeError as e:
            assert "append-only" in str(e), e


def test_legacy_json_is_migrated_exactly_once():
    """The mainnet history must survive the move, and never double-import."""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        legacy = tmp / ".lp_state.test.json"
        legacy.write_text(json.dumps({
            "status": "paused", "token_id": 7116214, "rebalance_count": 1,
            "gas_spent_wei": 31857000000000,
            "history": [{"at": "2026-08-11T16:15:32+00:00", "action": "rebalance"}],
            "snapshots": [{"ts": 1.0, "fees_usdt": 0.5, "fees_bnb": 0.001}],
        }))
        _isolated_store(tmp)
        state = s.load_state()
        assert state["token_id"] == 7116214 and state["rebalance_count"] == 1
        assert len(state["history"]) == 1 and len(state["snapshots"]) == 1

        # Re-opening must not duplicate: the guard is "DB is empty", not
        # "file is absent", so the legacy file staying put is safe.
        s._store.cache_clear()
        assert len(s._store().load()["history"]) == 1


def test_corrupt_legacy_json_refuses_rather_than_starting_fresh():
    """B10: silently starting fresh resets token_id to the toml bootstrap."""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        (tmp / ".lp_state.test.json").write_text('{"status": "paused", trunc')
        try:
            _isolated_store(tmp)
            raise AssertionError("accepted a corrupt legacy state file")
        except (RuntimeError, ValueError) as e:
            assert "unreadable" in str(e).lower() or "expecting" in str(e).lower(), e


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


def test_state_dir_is_relocatable_and_stays_per_network():
    """$LP_STATE_DIR moves state onto durable storage without letting two
    networks share one file.

    Guards the split: a host whose filesystem is not durable (an AgentCore
    microVM, a scale-to-zero container) loses the state file, and the state file
    is the single source of truth for token_id (B10). Relocating it is the fix,
    but only if the network stays in the FILENAME — point mainnet and testnet at
    one file and a mainnet rebalance inherits the testnet token_id.
    """
    import importlib
    import os

    with tempfile.TemporaryDirectory() as d:
        os.environ["LP_STATE_DIR"] = d
        try:
            mod = importlib.reload(s)
            assert mod.STATE_PATH.parent == Path(d), mod.STATE_PATH
            # The network is in the name, so the two chains cannot collide.
            assert mod.NETWORK in mod.STATE_PATH.name, mod.STATE_PATH
            # The lock rides along with the state file, not the old directory.
            assert mod.LOCK_PATH.parent == Path(d), mod.LOCK_PATH
        finally:
            del os.environ["LP_STATE_DIR"]
            importlib.reload(s)

    # Back to the default (alongside the agent) once the override is gone.
    assert s.STATE_PATH.parent == Path(s.__file__).parent, s.STATE_PATH


def test_monitor_is_opt_in_on_both_halves():
    """Neither process may start the loop by default.

    The flock in _rebalance_lock is a FILESYSTEM lock: it excludes a second
    process on the same host and is blind to one on another machine. If the
    seller and the monitor are split across hosts and either half defaults to
    running the loop, both rebalance and the B11 guard cannot fire. So the
    default must be off on both sides, and the check is that neither source
    starts it unconditionally.
    """
    for path, flag in (("main.py", "AGENT_RUN_MONITOR"),
                       ("../service/main.py", "SERVICE_RUN_MONITOR")):
        src = (Path(__file__).parent / path).read_text()
        assert "start_monitor()" in src, f"{path} no longer starts the monitor at all"
        gate = src.split("start_monitor()")[0]
        assert flag in gate, f"{path} starts the monitor without checking ${flag}"


def main() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"{len(tests)} passed")

# --- The LLM must not be handed a number it can misread as money --------------
def test_llm_tools_never_expose_a_raw_liquidity_integer():
    """B9, regressed via B21 and caught by a PAID job (56589).

    `get_position_summary` returned V3 liquidity and no TVL, so the model asked
    for TVL scaled the liquidity integer and delivered "TVL: 335,389.79 BNB"
    for a position holding $0.81. main.py already forbade exactly this in the
    prompt and named that exact figure — prompting did not hold, so the
    material is withheld instead.

    Offline: no RPC. Asserts the shape of the guard, not live chain values.
    """
    import tools

    payload = {
        "liquidity": {"position_liquidity": 335389792730626532,
                      "pool_active_liquidity": 1274497388494995892409666},
        "nested": [{"liquidity_raw": 123456789}],
        "price": 613.44,
        "token_id": 7116214,
    }
    safe = tools._hide_raw_liquidity(payload)

    assert isinstance(safe["liquidity"]["position_liquidity"], str)
    assert isinstance(safe["liquidity"]["pool_active_liquidity"], str)
    assert isinstance(safe["nested"][0]["liquidity_raw"], str)
    for text in (safe["liquidity"]["position_liquidity"],
                 safe["nested"][0]["liquidity_raw"]):
        assert "NOT a token amount" in text and "TVL" in text, text

    # Values that ARE money or identity must pass through untouched.
    assert safe["price"] == 613.44
    assert safe["token_id"] == 7116214


# --- spec 17/18 card fields must never fabricate a window ---------------------
def test_apr_is_none_when_it_cannot_be_annualised():
    """"0%" and "no data" are different claims about a yield."""
    assert s._apr_pct(1.0, 0, 100.0) is None        # no observed window
    assert s._apr_pct(1.0, 86400, 0.0) is None      # no TVL to yield ON
    # 1% of TVL earned in a day annualises to ~365%.
    apr = s._apr_pct(1.0, 86400, 100.0)
    assert abs(apr - 365.0) < 1e-6, apr


def test_observed_window_needs_two_samples():
    assert s._observed_window_seconds([]) == 0.0
    assert s._observed_window_seconds([{"ts": 100.0}]) == 0.0
    assert s._observed_window_seconds([{"ts": 100.0}, {"ts": 460.0}]) == 360.0


def test_pnl_30d_is_withheld_until_the_window_is_complete():
    """The agent has watched for days. A 30D figure would be a fabrication.

    Mirrors fees_24h_window_complete (B14): report the flag, not a number that
    silently means something narrower than its label.
    """
    snaps = [{"ts": time.time() - 3600, "fees_usdt": 0.0, "fees_bnb": 0.0}]
    _, complete = s._fees_since(snaps, s.THIRTY_DAYS, 1.0, 0.0, price=600.0)
    assert complete is False



if __name__ == "__main__":
    main()
