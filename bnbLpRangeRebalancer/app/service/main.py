"""Service Layer entrypoint (spec section 2).

Serves the spec section 8 REST API on :8080. Separate process from the Agent
Layer, which serves A2A JSON-RPC on :9000 — they share the state file and the
same ``strategy.py`` code, not a socket.

    python app/service/main.py          # this REST API      -> :8080
    python app/agent/main.py            # the A2A seller     -> :9000

## Which process runs the monitor loop

Exactly one, chosen explicitly. Neither process starts it by default:

    SERVICE_RUN_MONITOR=1   -> this service runs the loop
    AGENT_RUN_MONITOR=1     -> app/agent/main.py runs it instead

Set neither and nothing polls; both halves say so at startup, ``/health``
reports it, and ``strategy.is_monitor_running()`` answers it.

Setting BOTH on one host is wasteful but not dangerous: the ``flock`` in
``strategy._rebalance_lock`` refuses the second process's rebalance rather than
duplicating it, so funds are never double-moved — you just get noise.

Setting both across TWO HOSTS is dangerous, and is why neither defaults to on.
An flock is a filesystem lock: it cannot see a process on another machine, so
both monitors acquire it, both read the same liquidity, and both rebalance. The
guard is structurally unable to fire. If the seller and the monitor run on
separate hosts, exactly one of those hosts sets its flag.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parent.parent / "agent"
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

# `bag dev` auto-loads .studio/.env.local for the Agent Layer; this process is
# plain `python main.py`, so it did not — every run needed the variables
# exported by hand, and a forgotten SERVICE_API_KEY silently 503s the control
# routes. Same loader the SDK uses, so both halves read one file.
# Real environment wins: an explicit `FOO=1 python main.py` must still override.
from bnbagent_studio_core.config import env_local_path, load_env  # noqa: E402

_ENV_FILE = env_local_path(AGENT_DIR)
if _ENV_FILE.exists():
    load_env(_ENV_FILE)


def _configure_logging() -> None:
    log = logging.getLogger("seller-agent")
    if log.handlers:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.propagate = False


_configure_logging()
log = logging.getLogger("seller-agent.service")

if __name__ == "__main__":
    import uvicorn

    import risk
    import strategy as strat
    from api import app

    # Loud at boot, same as the agent: cross-field config errors are invisible
    # to per-field validation and only surface as a worthless signed quote or a
    # trade against the wrong position.
    for problem in risk.check_config_consistency():
        log.error("CONFIG: %s", problem)

    if os.environ.get("SERVICE_RUN_MONITOR") == "1":
        strat.start_monitor()
        log.info("monitor loop started by the service layer")
    else:
        log.info("monitor loop NOT started here: set SERVICE_RUN_MONITOR=1 to run "
                 "it in this process, or AGENT_RUN_MONITOR=1 to run it in the "
                 "A2A agent. Exactly one of the two, and never on two hosts.")

    if not os.environ.get("SERVICE_API_KEY"):
        log.warning("SERVICE_API_KEY unset — /activate, /pause and /execute will "
                    "refuse with 503. Read routes are unaffected.")

    uvicorn.run(
        app,
        host=os.environ.get("SERVICE_BIND_HOST") or "0.0.0.0",
        port=int(os.environ.get("SERVICE_PORT") or os.environ.get("PORT") or "8080"),
        log_level="info",
    )
