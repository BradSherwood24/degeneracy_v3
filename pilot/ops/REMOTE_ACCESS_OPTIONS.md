# Remote access to the hosted pilot -- options and decision notes (2026-10-16)

Status: NOTES, nothing built. Brad, 2026-10-16: "Security on the public port is essential. But lets hold for now.
... It'll become important once we're dealing with account balances in the six figure range, but the two figure
range of $70 has minimal risk inherently." Revisit before revoking the laptop Kalshi key, and before any size step.

## The problem

Since Test Fire #3 armed on Render (2026-10-16 02:12Z), Claude cannot see the worker. Every check of the ledger,
journals or supervisor log goes through Brad pasting Render Shell output. Kalshi ACCOUNT reads (balance, positions,
fills, settlements, resting orders) still work from the laptop, because the laptop proxy keeps its own key in
read-only mode (`ALLOW_ORDERS=false` since 2026-10-16 01:32Z) and the account is the same.

## Facts that constrain the design (checked 2026-10-16)

- **A Render background worker cannot receive traffic**, public or private. It can only send. So data has to be
  PUSHED off the worker; nothing can reach in and read its disk.
- **A Render disk attaches to exactly one service.** No second service can read `/var/dv3`.
- **Web services and private services can listen on extra ports** that other services reach over the private
  network (up to 75 ports; 18012, 18013, 19099 reserved). Public traffic routes to the main HTTP port only. The docs
  do not state outright that the extra ports are unreachable from the internet: prove it with an outside test.
- **Render cannot convert a private service into a web service.** Making the proxy public means a new service, a new
  internal hostname for the worker, and a redeploy, all in a :02-:33 band.
- **Render SSH** works for background workers and private services: an account-level public key (ed25519), the
  command from the service's Connect -> SSH tab, sessions drop on redeploy or restart. Ohio host key fingerprint
  `SHA256:kRDsLlrHqOyqso58sEKyO6ZFMPj7p24zfNxYJ42yXGI`.
- **Kalshi documents no read-only or scoped API keys.** Any key that can read the account can trade.
- **Worker disk growth:** 536 MB after ~11 h on 2026-10-16 (~1.2 GB/day if linear); the 20 GB disk would fill in
  about 2.5 weeks. Journals are ~15 MB per hour and are the research corpus (never deleted without a confirmed copy).

## Options

| | what | cost | security | notes |
|---|---|---|---|---|
| A | Brad pastes Render Shell output (today) | 0 | best: nothing new exposed | slow; Brad in the loop |
| B | Render SSH with a Claude key on the laptop | 0 | full shell on both services; no read-only mode | no build; heavy commands only :02-:33 |
| C | Worker pushes rows to Render Postgres; Claude reads with a read-only login | small paid DB plan | writer login insert-only; reader SELECT-only; TLS + inbound IP rules | rows only; journals do not belong in a DB |
| D | Keyless public "observer" web service | ~1 small service | signed reads (Claude's private key on the laptop, public key on the service); no Kalshi key on it; fixed reads forwarded to the private proxy | needs the worker push too |
| E | Make the proxy public, with an allowlisted read port | 0 extra services | the trading key's process faces the internet | port split: public port = allowlisted signed reads, private port = today's proxy |
| F | Worker pushes rows to a private GitHub repo | 0 | repo-scoped token on the worker | clumsy to query |

## Design notes kept for later

**Worker push (C, D, F all need it), Brad's flow:** after each window exits, the supervisor writes the new rows to an
outbox, pushes them, and on the next wake confirms the write and deletes the OUTBOX copy. A failed push is retried
alongside the next window's. Key each row by close_time so a retry that already landed is skipped (idempotent).
Never inside a window; short timeouts; a failed push never affects trading. The worker's own ledger stays on its disk
(the report and the stop checks read it); journals stay until a confirmed off-box copy exists.

**Public endpoint security (D or E), non-negotiable:**
- Every request signed with Claude's Ed25519 key held on the laptop (never in chat); the service holds only the
  public key, so a leak of its settings grants nothing.
- Signature over method, path and query, a timestamp and a one-time nonce; reject anything older than 30 s or
  replayed; failures return a bare 404; HTTPS; rate limit; no stack traces.
- Fixed endpoints only: balance, positions, resting orders, and fills / settlements for ONE named window whose
  close time must match a strict pattern and is converted to a time range. No request input ever chooses a Kalshi
  path. No write path exists in the public code at all.
- The worker's push uses a SEPARATE secret from Claude's read key.

**Why E is last:** since V2 the signing key has lived only on a box nothing outside can reach. E keeps the allowlist
but puts the key's process on the internet, so a bug in the public listener could expose a key that can trade. D gives
the same reads with no key on the public edge.

## Recommended path

1. **Now (two-figure balance):** Kalshi reads through the laptop proxy (works today). Worker checks via A.
2. **Next:** the journal offload, sized from `du -sh /var/dv3/*` on the worker, before the disk fills. Then C for
   the rows, built as one small worker PR plus a laptop reader.
3. **Before revoking the laptop key, or any large size step:** D, the keyless observer with signed reads.
   Historical market data keeps working through an unsigned laptop proxy after the laptop key is revoked.
4. **E only if a reason appears that D cannot meet,** and then only the port-split version with signed reads.
