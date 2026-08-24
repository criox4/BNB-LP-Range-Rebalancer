# Exposing an agent on a nip.io hostname

How the agents on the VPS (`172.104.171.139`) get a public HTTPS hostname without
owning a domain. Written after wiring up `bnb-yield-optimizer`; the same steps
apply to every agent that follows.

## What nip.io actually is

A public wildcard DNS service. There is no account, no dashboard, and no records
to create — its nameservers parse the IP out of the hostname you asked for and
return it:

```
<anything>.172-104-171-139.nip.io  →  172.104.171.139
```

The `172-104-171-139` label *is* the answer. Nothing propagates, so a new
subdomain resolves the instant you invent it. All the configuration is on our
side: nginx `server_name` plus a certbot run.

Use the dashed form. The dotted form (`bnb.172.104.171.139.nip.io`) is also
valid, but it makes every octet its own DNS label, which trips up TLS and cookie
logic that counts label depth.

### Two limits that shape the setup

- **No wildcard certificate.** `*.172-104-171-139.nip.io` would need a DNS-01
  challenge, which means writing a TXT record — impossible on a service with no
  records. So: one cert per subdomain, HTTP-01 only, which is why port 80 must
  stay open and unredirected until certbot has run.
- **`nip.io` is on the [Public Suffix List](https://publicsuffix.org)**, so
  `172-104-171-139.nip.io` counts as its own registered domain for Let's
  Encrypt's 50-certs-per-week limit. Four agents is not close to it.

nip.io is someone else's uptime. If it goes away, every hostname stops resolving
and any `ERC8183_AGENT_URL` published on-chain points at nothing. Fine for
testing; for an agent holding real funds, buy a domain **before** running
`register_erc8004.py`, because changing it later means re-registering on-chain.
`sslip.io` is a drop-in alternative with identical mechanics.

## Host port allocation

The agent container publishes to **loopback only** (`127.0.0.1:PORT:PORT`). A
bare `ports: "8003:8003"` would bypass ufw entirely — docker writes its own
iptables rules — and expose operator routes to the internet. nginx must be the
only reachable path in.

| Agent | Hostname | Host port |
|---|---|---|
| bnb-yield-optimizer | `bnb-yield.172-104-171-139.nip.io` | 8003 |
| bnb-lp-rebalancer (agent) | `bnb-lp.172-104-171-139.nip.io` | 9000 |
| bnb-lp-rebalancer (service API) | `bnb-lp-api.172-104-171-139.nip.io` | 8080 |
| *(unassigned)* | | 8004, 8005 |

## Procedure

### 1. Pick the hostname and write it into the nginx conf

The shipped conf is HTTP-only on purpose. `certbot --nginx` rewrites the block
in place to add `listen 443 ssl`, the cert paths, and the HTTP→HTTPS redirect —
and it can only do that against a config nginx already loads. A 443 block with
commented-out cert paths fails `nginx -t`, so certbot never gets to run.

```bash
sudo cp deploy/nginx/bnb-lp-rebalancer.conf /etc/nginx/sites-available/bnb-lp-rebalancer
sudo ln -sf /etc/nginx/sites-available/bnb-lp-rebalancer /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

Check `server_name` and the `upstream` port match the table above before
reloading.

### 2. Issue the certificate

```bash
sudo certbot --nginx \
  -d bnb-lp.172-104-171-139.nip.io \
  -d bnb-lp-api.172-104-171-139.nip.io \
  --redirect
```

Multiple `-d` flags on one run produce a single SAN cert covering both names —
fewer renewals than one cert per hostname. certbot installs its own systemd
timer; no cron needed.

### 3. Point the agent's own config at the same hostname

This is not optional. The hostname is published on-chain *and* the MCP
streamable-HTTP transport validates incoming `Host` headers against it — a
mismatch returns 421 on every `/mcp/` call and reads like a protocol fault.

In `.env.production`:

```ini
AGENT_DOMAIN=bnb-lp.172-104-171-139.nip.io
ERC8183_AGENT_URL=https://bnb-lp.172-104-171-139.nip.io/erc8183
MCP_ALLOWED_HOSTS=
```

Leave `MCP_ALLOWED_HOSTS` empty — it is derived from `ERC8183_AGENT_URL`
automatically. Only set it if something else also fronts the agent.

### 4. Trust the proxy headers

```ini
FORWARDED_ALLOW_IPS=172.16.0.0/12
```

Without this the A2A agent card advertises a **`http://`** origin, and a
discovering agent follows a plaintext URL that then 301s.

The card builds its origin from `request.base_url`, which derives the scheme
from `X-Forwarded-Proto`. uvicorn only honours that header from
`forwarded_allow_ips`, default `127.0.0.1` — but docker-proxy rewrites the
source address, so nginx arrives as the bridge gateway (`172.19.0.1`), the
header is dropped, and the scheme falls back to `http`. Trusting the private
bridge range is safe here precisely because the container port is loopback-only:
nothing but nginx can reach it.

### 5. Restart and verify

```bash
sudo docker compose up -d
```

```bash
BASE=https://bnb-lp.172-104-171-139.nip.io

curl -s $BASE/health
curl -s -o /dev/null -w '%{http_code} %{redirect_url}\n' http://${BASE#https://}/health   # expect 301
curl -s -o /dev/null -w '%{http_code}\n' -X POST $BASE/mcp/ \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"c","version":"1"}}}'
curl -s $BASE/.well-known/agent-card.json | jq -r '.provider.url, .supportedInterfaces[0].url'
```

The last command must print `https://` URLs. If it prints `http://`, step 4 did
not take effect.

### 6. Register on-chain

Only once the checks above pass — `register_erc8004.py` writes to BSC mainnet and
publishes whatever URL it is given.

```bash
sudo docker compose exec agent uv run python scripts/register_erc8004.py
```

## Notes

- A bare `GET /erc8183` returns 404 by design; it is a router prefix. The real
  route is `POST /erc8183/negotiate`.
- `postpilot` holds `default_server` on :443. Named `server_name` blocks win over
  it on an exact match, so no conflict — but an unmatched hostname lands on
  postpilot rather than erroring, which can make a typo'd `server_name` look like
  a routing bug.
- `nginx -t` warns about redefined `[::]:443` protocol options. Pre-existing,
  cosmetic: one site sets `http2` on that socket and another doesn't.
- Renewal is automatic. To rehearse it: `sudo certbot renew --dry-run`.

## Checking what exists

```bash
sudo certbot certificates                       # names, expiry
ls -l /etc/nginx/sites-enabled/                 # which sites are live
sudo ss -ltnp | grep 127.0.0.1                  # which ports are taken
```
