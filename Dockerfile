# One image, two entrypoints — the Agent Layer (A2A seller, :9000) and the
# Service Layer (REST, :8080). They are separate PROCESSES by design (spec 2),
# but not separate builds: app/service imports strategy.py / risk.py /
# blockchain.py straight out of app/agent, so their dependency sets are
# identical and two Dockerfiles would only be two ways to drift.
# docker-compose.yml runs this image twice with different commands.
#
# Build context is the REPO ROOT, not bnbLpRangeRebalancer/ — blockchain.py
# locates config/bsc-contracts.json by walking parent directories (spec 13),
# so the shared address book must be inside the image above the agent.
FROM python:3.13-slim

# curl is here for the compose healthchecks; nothing in the app shells out.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first so a code edit does not re-resolve the whole tree.
COPY bnbLpRangeRebalancer/app/agent/pyproject.toml /app/bnbLpRangeRebalancer/app/agent/
RUN pip install --no-cache-dir -e /app/bnbLpRangeRebalancer/app/agent \
    && pip install --no-cache-dir "fastapi>=0.110" "uvicorn[standard]>=0.27"

COPY config/ /app/config/
COPY bnbLpRangeRebalancer/app/ /app/bnbLpRangeRebalancer/app/

# Durable by default. Both are bind-mounted in compose; declaring them here
# means a plain `docker run` without -v still does not lose state on restart.
#   LP_STATE_DIR       — the strategy state file AND the flock. Ephemeral here
#                        means a rebalance history that resets on redeploy and
#                        a lock that cannot serialise anything (B10/B11).
#   STORAGE_LOCAL_PATH — ERC-8183 deliverable manifests. Losing these makes a
#                        paid job's deliverable_url 404 forever (B23).
ENV LP_STATE_DIR=/data/state \
    STORAGE_LOCAL_PATH=/data/deliverables
RUN mkdir -p /data/state /data/deliverables
VOLUME ["/data"]

# The keystore is NEVER baked in — see .dockerignore, which excludes .studio/.
# Mount it read-only at runtime and pass WALLET_PASSWORD from the host env.
# The variable name is the SDK's (wallet.KEYSTORE_DIR_ENV); without it the
# wallet loader falls back to a workspace-relative .studio/wallets that the
# image deliberately does not contain, and signing fails at the first quote.
ENV BNBAGENT_KEYSTORE_DIR=/secrets/wallets

# Run as a non-root user; /data must be writable by it.
RUN useradd --create-home --uid 10001 agent \
    && chown -R agent:agent /data
USER agent

# 9000 = A2A seller (AgentCore's contract), 8080 = REST service.
EXPOSE 9000 8080

# No default CMD: this image has two legitimate entrypoints and picking one
# silently would make `docker run` start the wrong half. compose sets both.
