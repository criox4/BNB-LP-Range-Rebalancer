/**
 * Quant decision loop for the LP Rebalancer's TermiX clients.
 *
 * WHAT THE SIGNAL IS, AND WHY IT IS THIS ONE
 * ==========================================
 *
 * This agent already maintains a concentrated-liquidity range on BNB/USDT and
 * recomputes, every 60s, where spot sits inside it — `range_utilization`, 0 at
 * the lower bound and 100 at the upper. That number is a ready-made statement
 * of "cheap or expensive relative to the band this agent has already committed
 * its own money to defend", and it costs nothing extra to read.
 *
 *     utilisation low   -> BNB near the bottom of the band -> buy WBNB
 *     utilisation high  -> BNB near the top                -> sell back to U
 *
 * It is mean reversion INSIDE a band the agent re-centres on every rebalance,
 * which is what keeps it from being a naive "buy the dip" that averages into a
 * trend forever: when price leaves the band the LP side re-ranges, and the
 * signal re-centres with it.
 *
 * This is NOT a claim that it makes money. It is a rule, it is deterministic,
 * and every decision it takes is explainable in one line at report time — which
 * is what a dispute is judged on.
 *
 * DECISION HERE, EXECUTION IN THE SKILL
 * =====================================
 *
 * This file never sees a private key and never signs. It decides buy/sell/hold
 * and shells out to `aacp-quant.mjs trade`, which decrypts the client envelope
 * in its own process, signs as the client's session and drops the plaintext.
 * The split means a bug in this strategy cannot leak a client's session key.
 *
 * GUARDS ARE ABOUT COST, NOT SAFETY
 * =================================
 *
 * The session's own limits — allowlisted contracts, daily cap, expiry — are
 * enforced on-chain and a call outside them REVERTS. Nothing here protects the
 * client from this agent; the account already does that. These guards exist
 * because a revert still burns the client's gas and reports nothing useful, and
 * because a 0.50% round trip on this pool eats any edge if it is paid to
 * express a signal that merely wobbled.
 *
 *   node monitor.mjs                 # decide and print, broadcast NOTHING
 *   node monitor.mjs --live          # actually trade
 *   node monitor.mjs --live --loop   # ... every INTERVAL_SECONDS, forever
 */
import { execFileSync } from "node:child_process";
import { readFileSync, writeFileSync, mkdirSync, existsSync } from "node:fs";
import { dirname, join } from "node:path";

const SKILL = process.env.QUANT_SKILL_DIR || "/root/BNBAgents/bnb-lp-quant/skill";
const AGENT_ID = process.env.TERMIX_AGENT_ID || "";
const STATUS_URL = process.env.LP_STATUS_URL || "http://127.0.0.1:8080/status";
const STATE_DIR = process.env.QUANT_STATE_DIR || "/root/BNBAgents/bnb-lp-quant/state";

// ── strategy parameters ─────────────────────────────────────────────────────
// A BAND, not a threshold. One line would flip the position every time
// utilisation jittered across it, paying 50 bps a round trip for noise.
const BUY_BELOW_PCT = Number(process.env.QUANT_BUY_BELOW ?? 35);
const SELL_ABOVE_PCT = Number(process.env.QUANT_SELL_ABOVE ?? 65);

// A round trip on the V2 pool costs ~0.50% in fees alone, plus gas. Below this
// notional the trade cannot pay for itself whatever the signal says: at 2.5 U
// the break-even move is ~1.0%, at 10 U ~0.6%.
const MIN_ENTRY_U = Number(process.env.QUANT_MIN_ENTRY_U ?? 2.5);

// Ladder the allocation instead of spending it in one go, so a single entry
// price cannot define the whole term.
const MAX_ENTRIES = Number(process.env.QUANT_MAX_ENTRIES ?? 4);

// Two trades closer together than this are almost always the same signal
// twice. 4h is the Guardian's figure and the reasoning is identical.
const MIN_HOLD_SECONDS = Number(process.env.QUANT_MIN_HOLD_SECONDS ?? 4 * 3600);

// Refuse a fill that moves the pool more than this. The pool is thin: 5,000 U
// one way is already 6%.
const MAX_IMPACT_PCT = Number(process.env.QUANT_MAX_IMPACT_PCT ?? 1.0);

// Flatten to U before the session dies. A term that ends with the client
// holding WBNB has handed them an unasked-for directional position that the
// agent can no longer close — the session key stops working at expiry.
const EXIT_BUFFER_HOURS = Number(process.env.QUANT_EXIT_BUFFER_HOURS ?? 12);

const INTERVAL_SECONDS = Number(process.env.QUANT_INTERVAL_SECONDS ?? 300);

const LIVE = process.argv.includes("--live");
const LOOP = process.argv.includes("--loop");

const now = () => new Date();
const iso = () => now().toISOString();
const log = (...a) => console.log(iso(), ...a);

function statePath(jobId) {
  return join(STATE_DIR, `${jobId}.json`);
}

/** Per-job ledger. Two clients must never share one, or their caps merge. */
function loadState(jobId) {
  const p = statePath(jobId);
  if (!existsSync(p)) return { entries: [], trades: [], lastTradeAt: null };
  try {
    return JSON.parse(readFileSync(p, "utf8"));
  } catch (e) {
    log(`WARN unreadable state for ${jobId} (${e.message}); starting empty`);
    return { entries: [], trades: [], lastTradeAt: null };
  }
}

function saveState(jobId, s) {
  mkdirSync(dirname(statePath(jobId)), { recursive: true });
  writeFileSync(statePath(jobId), JSON.stringify(s, null, 2));
}

/** One skill command. WALLET_KEY is inherited, never placed on argv. */
function skill(args, { timeout = 180_000 } = {}) {
  const out = execFileSync("node", [join(SKILL, "scripts", "aacp-quant.mjs"), ...args], {
    cwd: SKILL,
    encoding: "utf8",
    timeout,
    env: process.env,
  });
  return JSON.parse(out);
}

async function lpSignal() {
  const res = await fetch(STATUS_URL, { signal: AbortSignal.timeout(20_000) });
  if (!res.ok) throw new Error(`LP status ${res.status}`);
  const s = await res.json();
  if (s.range_utilization == null) throw new Error("LP status has no range_utilization");
  return {
    utilisation: s.range_utilization,
    price: s.current_price,
    inRange: s.in_range,
    // Out of range means spot has left the band the signal is measured against,
    // so utilisation is pinned at 0 or 100 and says nothing new. The LP side
    // will re-range; until it does, this strategy stands down.
    usable: s.in_range === true,
  };
}

/**
 * Price impact of `amountU` through the V2 pool, in percent, from reserves.
 * Read directly rather than trusted from config: the pool is the constraint,
 * and its depth changes.
 */
async function priceImpactPct(chain, amountU, side) {
  const call = async (to, data) => {
    const r = await fetch(chain.rpcUrl, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "eth_call", params: [{ to, data }, "latest"] }),
      signal: AbortSignal.timeout(20_000),
    });
    return (await r.json()).result;
  };
  const pad = (a) => a.toLowerCase().replace("0x", "").padStart(64, "0");
  const pair = "0x" + (await call(chain.factory, "0xe6a43905" + pad(chain.uToken) + pad(chain.wbnb))).slice(26);
  if (/^0x0+$/.test(pair)) throw new Error("no V2 pair for U/WBNB");
  const reserves = await call(pair, "0x0902f1ac");
  const token0 = ("0x" + (await call(pair, "0x0dfe1681")).slice(26)).toLowerCase();
  const r0 = Number(BigInt("0x" + reserves.slice(2, 66))) / 1e18;
  const r1 = Number(BigInt("0x" + reserves.slice(66, 130))) / 1e18;
  const uIsFirst = token0 === chain.uToken.toLowerCase();
  const [inRes, outRes] = side === "buy"
    ? (uIsFirst ? [r0, r1] : [r1, r0])
    : (uIsFirst ? [r1, r0] : [r0, r1]);
  const afterFee = amountU * 0.9975;
  const out = (afterFee * outRes) / (inRes + afterFee);
  const spot = (amountU * outRes) / inRes;
  return ((spot - out) / spot) * 100;
}

function spentToday(state) {
  const cutoff = Date.now() - 86_400_000;
  return state.trades
    .filter((t) => t.side === "buy" && new Date(t.at).getTime() >= cutoff)
    .reduce((a, t) => a + Number(t.amount), 0);
}

function heldWbnb(state) {
  return state.entries.reduce((a, e) => a + Number(e.wbnb || 0), 0);
}

/** buy / sell / hold / flatten, with the sentence that explains it. */
function decide(job, state, signal) {
  const expiresIn = (new Date(job.sessionExpiresAt).getTime() - Date.now()) / 3.6e6;

  if (job.revokedAt) return { action: "hold", why: "the client revoked the session" };
  if (expiresIn <= 0) return { action: "hold", why: "the session has expired" };

  // Flatten first: it outranks every other rule, including the minimum hold. A
  // position the agent cannot close is worse than one closed slightly early.
  if (expiresIn <= EXIT_BUFFER_HOURS) {
    return state.entries.length
      ? { action: "sell", why: `session expires in ${expiresIn.toFixed(1)}h — returning to U` }
      : { action: "hold", why: `session expires in ${expiresIn.toFixed(1)}h — flat, nothing to close` };
  }

  if (!signal.usable) {
    return { action: "hold", why: "price is outside the LP range; the signal is pinned and says nothing" };
  }

  if (state.lastTradeAt) {
    const held = (Date.now() - new Date(state.lastTradeAt).getTime()) / 1000;
    if (held < MIN_HOLD_SECONDS) {
      return { action: "hold", why: `minimum hold not met (${(held / 3600).toFixed(1)}h of ${(MIN_HOLD_SECONDS / 3600).toFixed(0)}h)` };
    }
  }

  const u = signal.utilisation;

  if (u >= SELL_ABOVE_PCT) {
    return state.entries.length
      ? { action: "sell", why: `utilisation ${u.toFixed(1)}% — at the top of the range, taking profit into U` }
      : { action: "hold", why: `utilisation ${u.toFixed(1)}% says sell but the position is already flat` };
  }

  if (u <= BUY_BELOW_PCT) {
    if (state.entries.length >= MAX_ENTRIES) {
      return { action: "hold", why: `utilisation ${u.toFixed(1)}% says buy but all ${MAX_ENTRIES} entries are open` };
    }
    return { action: "buy", why: `utilisation ${u.toFixed(1)}% — at the bottom of the range, rotating U into WBNB` };
  }

  return { action: "hold", why: `utilisation ${u.toFixed(1)}% is inside the ${BUY_BELOW_PCT}–${SELL_ABOVE_PCT}% band` };
}

function buySize(job, state) {
  const allocation = Number(job.allocationU);
  const dailyCap = Number(job.dailyCapU);
  const perEntry = allocation / MAX_ENTRIES;
  const remainingToday = dailyCap - spentToday(state);
  const deployed = state.entries.reduce((a, e) => a + Number(e.amountU), 0);
  const remainingAllocation = allocation - deployed;
  const size = Math.min(perEntry, remainingToday, remainingAllocation);
  if (size < MIN_ENTRY_U) {
    return { size: 0, why: `size ${size.toFixed(4)} U is below the ${MIN_ENTRY_U} U floor (daily left ${remainingToday.toFixed(2)}, allocation left ${remainingAllocation.toFixed(2)})` };
  }
  return { size: Number(size.toFixed(6)), why: "" };
}

async function evaluateJob(job, chain, signal) {
  const jobId = job.id || job.quantJobId;
  const state = loadState(jobId);
  const d = decide(job, state, signal);

  if (d.action === "hold") {
    log(`[${jobId}] hold — ${d.why}`);
    return;
  }

  let side = d.action;
  let amount;

  if (side === "buy") {
    const { size, why } = buySize(job, state);
    if (!size) {
      log(`[${jobId}] hold — ${why}`);
      return;
    }
    amount = size;
  } else {
    amount = Number(heldWbnb(state).toFixed(8));
    if (!(amount > 0)) {
      log(`[${jobId}] hold — nothing held to sell`);
      return;
    }
  }

  // Impact is checked on the U-notional either way: for a sell that is the
  // approximate value coming back, which is what the ceiling is about.
  const notional = side === "buy" ? amount : amount * signal.price;
  const impact = await priceImpactPct(chain, notional, side);
  if (impact > MAX_IMPACT_PCT) {
    log(`[${jobId}] hold — ${side} of ${notional.toFixed(2)} U notional would move the pool ${impact.toFixed(2)}% (ceiling ${MAX_IMPACT_PCT}%)`);
    return;
  }

  log(`[${jobId}] ${side.toUpperCase()} ${amount} — ${d.why} (impact ${impact.toFixed(3)}%)`);

  if (!LIVE) {
    log(`[${jobId}] DRY RUN — nothing broadcast. Re-run with --live.`);
    return;
  }

  const result = skill(["trade", "--agent", AGENT_ID, "--job", jobId,
                        "--side", side, "--amount", String(amount)]);
  log(`[${jobId}] submitted ${result.txHash || result.callsId} out=${result.expectedOut}`);

  const at = iso();
  state.trades.push({ at, side, amount, txHash: result.txHash ?? null,
                      expectedOut: result.expectedOut, why: d.why });
  if (side === "buy") {
    state.entries.push({ at, amountU: amount, wbnb: Number(result.expectedOut),
                         price: signal.price, utilisation: signal.utilisation });
  } else {
    state.entries = [];
  }
  state.lastTradeAt = at;
  saveState(jobId, state);
}

async function tick() {
  if (!AGENT_ID) throw new Error("TERMIX_AGENT_ID is not set");
  if (!process.env.WALLET_KEY) throw new Error("WALLET_KEY is not set");

  const signal = await lpSignal();
  log(`signal: utilisation ${signal.utilisation.toFixed(1)}% price ${signal.price.toFixed(2)} inRange=${signal.inRange}`);

  const chain = await resolveChainViaConfig();

  const inbox = skill(["inbox", "--agent", AGENT_ID]);
  const rows = inbox.items || [];
  if (!rows.length) {
    log("no client sessions");
    return;
  }
  for (const row of rows) {
    const job = skill(["job", "--job", row.quantJobId]);
    if (job.status !== "ACTIVE") {
      log(`[${row.quantJobId}] skip — status ${job.status}`);
      continue;
    }
    await evaluateJob(job, chain, signal);
  }
}

/** Venue addresses, live from the backend — never hardcoded here. */
async function resolveChainViaConfig() {
  const res = await fetch("https://platform-backend-bnb8183.prod.termix.live/api/v1/config/contracts",
                          { signal: AbortSignal.timeout(20_000) });
  const cfg = await res.json();
  const q = cfg.quant ?? cfg.data?.quant;
  if (!q) throw new Error("backend published no quant config");
  return {
    rpcUrl: process.env.BSC_RPC || "https://bsc-rpc.publicnode.com",
    factory: "0xcA143Ce32Fe78f1f7019d7d551a6402fC5350c73",   // PancakeSwap V2 factory
    uToken: q.token.address,
    wbnb: (q.tradableTokens || []).find((t) => t.symbol === "WBNB").address,
    router: (q.venueAllowlist || []).find((v) => v.kind === "venue").address,
  };
}

async function main() {
  log(LIVE ? "LIVE — trades will be broadcast" : "dry run — nothing will be broadcast");
  for (;;) {
    try {
      await tick();
    } catch (e) {
      log(`ERROR ${e.message}`);
    }
    if (!LOOP) return;
    await new Promise((r) => setTimeout(r, INTERVAL_SECONDS * 1000));
  }
}

main();
