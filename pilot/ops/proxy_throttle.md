# Proxy change proposal — leaky-bucket write throttle with a `wing` whitelist

**Status: PROPOSAL ONLY. Do NOT apply from this repo.** `degeneracy-proxy/proxy.py` lives on Brad's box;
only Brad edits and restarts it (house law: agents never touch proxy key material / .env / *.pem). This
file records the change so Brad can apply it deliberately. It is Brad's idea, enabled by the `X-DV3-Class`
header the V3.3 async writer now sends (2026-09-30).

## The finding (why this is worth having)

The client already paces writes with a Basic-tier token bucket (`service.v33.executor.WriteTokenBucket`,
100 tokens/s, `COST_CREATE = COST_AMEND = 10`, `COST_CANCEL = 2`, a batch of N creates = `10*N`). That
bucket is per-PROCESS. Two rosters share ONE Kalshi account (V3.2 + V3.3), and on a trending window the
ladder re-places on every bucket change (~11 creates per switch, ~450 creates in 9 minutes on 2026-09-30),
so the account-wide write rate can approach Kalshi's tier limit and earn a `429`. A `429` on a **wing IOC
take** is the dangerous one: it delays the hedge on a rung that already filled (one-legged risk). A
proxy-side throttle that **queues** (delays), never rejects, and **never throttles a wing**, makes the
account-wide rate safe while keeping the safety-critical leg first.

## The rate to key on

Kalshi's per-account write budget is the Basic-tier **100 write-tokens/second** the client pacer already
models. The proxy should enforce the SAME model account-wide (across both rosters), as a leaky bucket:

- refill `RATE = 100` tokens/s, capacity `SIZE = 100` (one second of headroom);
- **cost table (identical to the client pacer):** order **create** = `10`; order **amend** = `10`; order
  **cancel** = `2`; a **batch create** body of N orders = `10 * N` (the proxy already parses the batch body
  to cap `max_contracts_per_order`, so it can count `len(body["orders"])` — see `proxy_phase_h.md` /
  the proxy README for the batch-body cap). A GET costs `0` (reads are a separate, uncapped budget today).

## The change (leaky bucket, queue-not-reject, wing whitelist)

At the point the proxy has classified a non-GET write (after the existing cap/budget checks, before the
upstream signed request):

1. **Read the class header.** `klass = request.headers.get("X-DV3-Class")` — one of `wing | rest | roll |
   cancel | poll`. (The V3.3 async writer sends it on every write; a write without the header is treated
   as non-whitelisted.)

2. **Whitelist `wing` — never throttled.** If `klass == "wing"`, skip the throttle entirely: the wing
   take / print-through complete / unwind is the safety-critical hedge and must go out immediately. (It
   still passes the existing `max_contracts_per_order` / budget caps.)

3. **Leaky-bucket DELAY for everything else.** Compute this write's `cost` from the table above. If the
   bucket lacks `cost` tokens, `sleep((cost - tokens) / RATE)` (async if the proxy is async) until it
   does — i.e. **queue, do not reject**. Then subtract `cost` and forward upstream. Refill `tokens =
   min(SIZE, tokens + elapsed * RATE)` on each write. A single mutex/lock around the bucket keeps it
   account-wide and correct across concurrent connections.

4. **Never a rejection.** The throttle must never return a `4xx`/`5xx` of its own — a delayed write is a
   delayed write, not a failure; the client's idempotency (client_order_id) assumes a write that is sent is
   sent once. Only the delay is added.

## Notes / caveats

- **Cancels stay cheap and fast** (cost 2), so a T-5 cancel-all is not meaningfully delayed even under load.
- **Reads (`poll`) are not throttled here** (GETs are the market-data host, uncapped today); if Kalshi ever
  meters reads too, add a second bucket keyed on `klass == "poll"`.
- **Header forwarding (unverified).** `X-DV3-Class` rides only the localhost hop; the proxy builds and signs
  its OWN upstream request, so a client header is not forwarded to Kalshi by construction — but the proxy
  source was not read here, so Brad should confirm the proxy does not echo arbitrary client headers upstream
  before relying on that. The header is harmless today (the proxy ignores unknown headers).
- **Client pacer stays.** This proxy throttle is the account-wide backstop; the per-process
  `WriteTokenBucket` stays as the first line (and its `wing`/`cancel` priority already mirrors the whitelist).
- **Nothing forces this change; the pilot is correct without it** — the client pacer + the `429` belt (retry
  once on the same lane) already handle throttling. This change makes the account-wide rate deliberate and
  keeps wings first when both rosters are busy.
