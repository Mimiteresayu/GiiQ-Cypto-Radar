# GiiQ SoT changelog

The executor rule set carries a version id, `exec_common.SOT_ID`. It is shown in the cockpit
header, the executor / pending run reports, the preflight JSON and `[DESK_DATA]`.

- The id only changes when an **executor rule** changes: entry/exit triggers, sizing, leverage or
  a guardrail. Bump the trailing number (`GIIQ-SoT-2`, ...) and add a section here in the same PR.
- The strategy itself (locked tier exits, GC math, size bands) is defined in
  `COMPLETE_LOGIC_LOCKED.md`, `SIZE_TIER_EXIT_LOCKED.md`, `ENTRY_APPROVAL_GATE.md` and
  `UNIVERSE.md`.
- Nothing changes rules automatically. Only MMT changes the SoT.

---

## GIIQ-SoT-5 (2026-10-06, MMT decision 2026-10-06 21:00 HKT)

Remove unnecessary ENTRY gates, add real safety protections from Signum platform. Exit logic unchanged.

### REMOVED gates

1. **HL daily 3-new-fills cap** (`MAX_NEW_ENTRIES_PER_DAY` removed). Owner: "no need limitation, as many as we can until total cap reach". The combined 80% NAV margin cap is the real protection.
2. **BX 1-entry-per-day and approve_max=1** (`MAX_NEW_PER_DAY`, approval rules in BX decisions). Same reasoning as HL.
3. **BX max-2-open-positions limit** (`MAX_OPEN`). Replaced by BX total margin cap (ADD-2 below).
4. **BX $2M 24h-volume floor** (`VOL_MIN`). Kept spread limit, volume-based sizing, and minimum quantity checks.
5. **Dead BTC bearish 4% sizing code** (`SIZE_BANDS`, `BTC_BEARISH_FIXED_SIZE_PCT`, `_clamp_size_leverage`). The live path never called it; removed entirely.
6. **V1–V8 rule IDs as blockers in code**. These are now statistics tags only; the desk prompt will be updated so V1–V7 and dimension scores never veto an order in the prompt either (tags only, default APPROVE).

### CHANGED rules

1. **Minimum leverage 2x** (was 3x). MMT rule: leverage 2x–5x by confidence; never above coin max leverage. `SOT2_MIN_LEV = 2`, leverage step-down now goes 5x → 2x.
2. **One total margin cap: 80% NAV** (replaces 70% margin + 80% utilisation pair). `MAX_TOTAL_MARGIN_NAV_PCT = 80.0`. HL+BX combined if the code can see both; otherwise apply 80% per venue.
3. **Minimum order: $10 only** (drop the extra 1%-of-NAV minimum). `min_order_usd` returns only `MIN_NOTIONAL_USD`.
4. **Process approvals in desk priority order** (Chase first, then alphabetical), not pure alphabetical. Executor iterates `approved_list` sorted by candidate type and symbol.
5. **Fallback approves every executable candidate at floor size** (2% margin, 2x leverage), not just Base. Chase still becomes pending, not an immediate entry. `build_fallback_decisions` approves all with `size_pct=2.0, leverage=2`.
6. **BX size/leverage from desk decision** (within 2x–5x bounds), not fixed 1%/3x. `check_entry` reads `approval.get("size_pct")` and `approval.get("leverage")`, clamps to 2–5x and coin max leverage. Pending Chase fills also use desk size/leverage from the stored approval.

### ADDED protections (Signum parity)

1. **ADD-1: BX radar data freshness and minimum row-count check**. Before any BX order, `bx_radar_fresh` checks:
   - 1D radar age ≤ 36h, 4H radar age ≤ 4.5h
   - Row count ≥ 70% of normal (120 rows baseline)
   - Fail-closed: skip all BX entries with `[BX_ALERT]` log if check fails.

2. **ADD-2: BX total margin cap**. Sum of isolated margin of open BX positions + new ≤ 80% NAV. `BX_TOTAL_MARGIN_CAP_PCT = 80.0`. This replaces the removed max-2-open limit with a real margin-based cap. Checked in `check_entry` with `bx_margin_used` passed in.

3. **ADD-3: HL post-fill reconciliation**. After each HL fill, `_reconcile_position` re-reads `clearinghouseState` and `open_orders` and verifies:
   - Position size matches filled size
   - Isolated margin mode (not cross)
   - Leverage matches requested
   - Liquidation price is beyond Hard SL
   - SL order is resting
   - Updates `cum_margin` with exchange's `totalMarginUsed`
   - On any mismatch: fix if possible (place missing SL) or close + alert. If close fails, status is `reconcile_failed_CLOSE_FAILED` (manual action required).

4. **ADD-4: Deterministic client order IDs** (idempotent orders). Prevents double-sends on retries:
   - HL: `cloid` from `_make_cloid(coin, HKT_date, "entry")` → hash-based, e.g. `giiq<sha256[:16]>`
   - BX: `clientId` with same deterministic scheme
   - A rerun or retry on the same day produces the same cloid, preventing duplicate orders.

5. **ADD-5: Conservative NAV for sizing**. `nav_snapshot` now returns `nav = min(equity_nav, free_usdc + margin_used)`:
   - `equity_nav` = the existing definition (spot USDC total for unified, or perp accountValue + spot for split)
   - `conservative_nav` = free USDC + isolated margin collateral
   - Sizing uses the minimum of the two, so NAV is conservative in both directions.

### NOT changed (hard constraints)

- **Exit logic**: `exit_worker`, 1H/4H channel exits, tier exit timeframes, Hard SL placement and levels, CONT Hard SL level. No item moves a stop.
- **Hard floors**: exchange Hard SL on every entry, total margin cap, kill switch, liquidation price must be beyond Hard SL, data-integrity checks (stale/missing data, stop above entry, coin max leverage, exchange minimums), price sanity check.
- **Left as-is (owner hasn't decided)**: Chase/CONT pending freeze, Chase immediate entry, Base 1D-Green requirement, live-mid > 1D Upper check, size clamp >4%, Tiny tier 3x/2%, 20% per-coin notional cap, BTC regime rule, API key handling, **minimum SL distance 1.5%** (Cove suggested 0.6% based on IOC slippage + fees, not approved by MMT).
- **Never touched**: `secrets/.env`, no live orders, no Railway changes, no deploys.

---

## Cove HEALTH FAIL 2026-10-05: CONTINUATION / ADD_ON disabled + decision-day candidate freeze
Cove sign-off `cove/bo_health_sign_2026-10-05.md` (decisions (a) + (b)); Forge diagnosis
`forge/bo_health_gate_2026-10-05.md`. SoT id left at `GIIQ-SoT-4`: this turns a path **off**
behind a flag and does not change any Base / sizing / exit rule. MMT may bump it at merge.

**(a) CONTINUATION / ADD_ON pending disabled (default ON in code).**
- Flag `PENDING_CONTINUATION_DISABLED` (`pending_entries.pending_disabled()`): unset/`1` = disabled;
  only `0` / `false` / `no` / `off` re-enables. Covers both CONTINUATION and ADD_ON.
- Executor 08:55: a Chase approval is **acknowledged, no entry**: result/run-report `no_entry`
  (not a skip), no pending is created (LIVE or DRY_RUN). Base entries unchanged.
- Active CONTINUATION / ADD_ON records are cancelled with
  `close_reason = "disabled by Cove HEALTH FAIL 2026-10-05"`: at cockpit boot (`serve.main`, so
  deploy + restart clears them), in the executor (LIVE) and in the 4H `:10` pending worker (LIVE).
  While disabled, the pending worker never evaluates bars or places orders. DRY_RUN never mutates the store.
- Preflight: `pending_mode` line; Chase approvals show OK `Chase -> no entry (... disabled ...)`.
  `POST /api/ai/decision` response lists `chase_no_entry`; `/api/exec/pending` has `disabled`.
- Not changed: Base entry path, Hard SL placement / exits on open positions (CHIP), margin caps,
  kill switch, HL keys, Railway-only orders.
- Re-enable (only after Cove re-signs CONTINUATION health): set `PENDING_CONTINUATION_DISABLED=0` on the
  cockpit service and redeploy. Cancelled records are never re-armed; new Chase approvals create new pendings.

**(b) Decision-day candidate freeze.**
- `entry_candidates_latest.json` keeps regenerating (4H/1H scans, live radar) for the ENTRY tab, but
  approvals are now matched against `out/entry_candidates_decision_YYYYMMDD.json` (HKT date):
  written by the scheduled 08:05 scan (overwrite), else by the first AI decision POST / 08:50 fallback
  of the day (only if missing). Only a list built today HKT is frozen.
- Executor + preflight use the snapshot; if it is missing they fall back to latest with a clear log
  (`candidates_source`). The candidate freshness guard still runs on latest (never less strict).
- Preflight: an approval missing from the list with an **active** CONTINUATION / ADD_ON pending is OK
  `already pending <kind>` (was WARN `approved but not in the current candidate list`). While disabled,
  a missing approval whose decision type is CHASE is OK `no entry` too.
- Tests: `test_pending_disabled.py` (the suite runs with `PENDING_CONTINUATION_DISABLED=0` via
  `conftest.py` so the existing CONTINUATION / ADD_ON rules stay covered).

---

## GIIQ-SoT-4 (2026-10-03, MMT decision 2026-10-03 13:35–13:45 HKT via Harbor; AIQ-0022)
HL executor + pending worker (shared `exec_common`). Bitunix (`bx_live.py`) is not touched: it has its
own sizing (1% NAV / 3x pilot) and its caps are AIQ-0003.
- **Total isolated margin ≤ 70% of NAV** (was 30%), cumulative across the run (existing + new).
  `MAX_TOTAL_MARGIN_NAV_PCT = 70.0`. The **80% margin-utilization cap stays as the outermost hard
  cap** (`MAX_MARGIN_UTILIZATION_PCT = 80.0`, unchanged). Coin notional ≤ 20% NAV unchanged.
- **Tiny tier: max 3x leverage and max 2% NAV margin per trade** (`TINY_MAX_LEV = 3`,
  `TINY_MAX_MARGIN_PCT = 2.0`). With the SoT-2 floors (3x / 2%) a Tiny trade is 3x / 2%. Tiny =
  mcap < $200M or unknown, so any tier other than mega/large/small is treated as Tiny. A Tiny ADD_ON
  whose existing leverage is above 3x is refused. Mega/Large/Small unchanged (3–5x, 2–4%).
- Code: `exec_common.size_by_margin(..., tier=)`, called with the candidate / pending tier by
  `executor.py`, `pending_worker.py` and the `serve.py` suggestion. `tier=None` keeps the old behaviour.
- Not changed: Hard SL / exits, HL −3% breaker (not added), SL re-align both ways, BTC gate (none).
- Tests: `test_aiq0022_margin70_tiny.py`.

---

## Bitunix live pilot (2026-09-30, MMT-approved; HL SoT id unchanged: no HL executor rule changed)
Separate rule set for Bitunix only — see `docs/BX_LIVE_PILOT.md`. Runs in the Singapore `bx-exec` service.
- Fail closed: non-US egress (two geo sources, Railway region, static IP), key present + signed read OK,
  BX_ENABLED=1 and BX_LIVE=1 (default 0), breaker not tripped, ENTRY_DESK approval (no fallback).
- 1% NAV isolated margin, 3x; ≤ 0.5% of 24h vol; max 2 open, max 1 new entry per day; entry tier only
  (≥ $2M, < 10 bp, 1D/4H GC, crypto only). Hard SL attached on the exchange; liq must sit below it.
- Exits: 4H close < 4H Filter, 7-day time cap (5 for new tokens), liquidity exit, exchange Hard SL.
- Circuit breaker at −3% NAV cumulative live P&L → no new entries, BX_BREAKER on the exit health, BX_LIVE=0.
- Cockpit: `/api/ai/candidates` gets a separate `bx` section; `/api/ai/decision` forwards `bx_decisions` to
  bx-exec (never into the HL decisions store); cockpit BX jobs stop when `BX_SERVICE_URL` is set.

---

## Bitunix shadow radar + reporting (2026-09-29, SoT id unchanged: no executor rule changed)
Approved by Harbor 2026-09-29 (design doc: Bitunix Universe Expansion). Display and shadow only.
- New jobs (own lock, subprocess without `HL_API_PRIVATE_KEY`): BX daily 08:20 HKT, 4H at :25, 1H at :27.
  No Bitunix key and no Bitunix orders. Files: `out/bx_*`, `out/cg_*`, ledger `out/bx_shadow_ledger.db`.
- The HL order path never reads BX data (`test_bx_isolation.py`); `[DESK_DATA]` and ENTRY_DESK candidates are unchanged.
- `/api/exit/health`: `info` PENDING_SKIPPED plus a note in the daily summary when a 4h pending check was skipped
  because that run's exits failed (accepted 4h delay, now reported). Also written to the daily run report.
- Docstrings fixed: pending entries are checked every 4h (not hourly); total margin cap is 30% NAV (not 80%).

---

## Reporting-only update (2026-09-28, SoT id unchanged: no executor rule changed)
- Veto rule ids (`rule`, e.g. V1_WEAK_4H_BREAKOUT) are stored with each decision and in the ledger;
  `/api/dimensions/report` has `veto_rules`: signals blocked per rule and how they did vs approved ones.
- `/api/exit/health`: leverage below 3x is `info` (LEVERAGE_LOW), not a problem; above 5x or non-isolated
  is still LEVERAGE_OFF.
- `POST /api/ai/jobs/run` (AI key): starts shadow jobs only (`dims`, `dims_outcomes`, `dims_backfill`).
- `dims_backfill`: historical replay of Base/Chase signals from the candle caches into a separate
  ledger (`giiq_ledger_backfill.db`); report with `/api/dimensions/report?db=backfill`.

---

## GIIQ-SoT-3 (2026-09-28): portfolio caps, price-based ADD_ON, fallback + POST alert (approved by MMT)

Entry and exit **triggers are unchanged**, and so is per-trade risk (isolated margin 2–4% NAV, 3–5x).

### 1. Portfolio caps (Base 08:55 and pending fills)
- **Total isolated margin ≤ 30% of NAV**, cumulative across the run (existing + new). The old 80%
  utilization check stays as an outer bound.
- **One coin's notional ≤ 20% of NAV**, including after an ADD_ON.
- **At most 3 new fills per HKT day**, counting Base, CONTINUATION and ADD_ON together (LIVE fills
  in the trade log + fills in the current run).

### 2. ADD_ON gate: price gain, not leveraged ROE
- The base position's **price gain vs entry must be ≥ +10%** (live mid ÷ entryPx − 1), which is
  Signum's 1x meaning. At 3–5x, +10% ROE was only a +2–3.3% move.
- The 5.5% coin-margin cap is replaced by the 20% NAV coin-notional cap. Margin room =
  (20% − current coin notional %) ÷ existing leverage, capped at 4%. Room below 2% → no add.

### 3. Decisions: fallback and missing-POST alert
- `POST /api/ai/decision` accepts the ENTRY_DESK shape `{coin, action: APPROVE|VETO, type, dims}` as
  well as `{symbol, decision}`. If nothing valid is sent it returns **422** and logs loudly (it used
  to return 200 with 0 stored).
- `"source": "fallback"` (Harbor 08:40) is **ignored when Claude already posted today (409)**.
  Fallback approvals execute as **Base only at 2% margin**; Chase / pending adds are skipped.
- The executor reports `claude_post.missing_days`: 1 → alert, **≥ 2 consecutive days → RED alert**.
- Decisions received after 08:50 HKT are stored but flagged `late`.
- **08:50 HKT Railway fallback** (`decision_fallback` job): if no Claude decision is stored today,
  Railway stores fallback decisions itself (Base → approve at 2%, Chase → veto), so nothing depends
  on a desktop or an outside agent. `AUTO_FALLBACK=0` turns it off (then no Claude POST = no trade).

### 4. EXIT health (report only)
`GET /api/exit/health` (keyed) and `exit_health` in `[DESK_DATA]`: NO_SL, EXIT_NOT_DONE, JOB_FAILED,
JOB_MISSED, MARGIN_HIGH (> 80%), ORPHAN_SL, PENDING_STALE, LEVERAGE_OFF, RADAR_STALE, HL_FETCH.
`summary` = "OK · n 倉" when clean.

---

## GIIQ-SoT-2 (2026-09-28): sizing / leverage / ADD_ON rule change (approved by MMT)

Entry and exit **triggers are unchanged**. These rules apply to Base entries (08:55 executor and
`/api/exec/run`) and to pending fills (ADD_ON / CONTINUATION, 4H :10 job).

### 1. ADD_ON pending fills: extra gates at fill time
- The existing position's **ROE must be ≥ +10%**. ROE = HL `returnOnEquity`, falling back to
  uPnL ÷ marginUsed.
- The coin's **total margin after the add must be ≤ 5.5% of NAV** (per-run NAV snapshot):
  existing `marginUsed` + new margin ≤ 5.5%.
  - The new margin is capped by the room left under 5.5%.
  - If the room is below the 2% minimum margin, the add is skipped.
- A failed gate means no order. The record stays pending, and the reason appears in the daily
  run report (`skipped`).

### 2. Margin = risk, leverage 3–5x isolated (replaces the flat 2x and the old size bands)
MMT corrected the first draft of this rule on 2026-09-28. **Per-trade risk is the isolated margin
itself, not a size based on SL distance.** SL and exits are dynamic (tier-based), so SL-risk caps
were dropped: there is **no** "1.5% NAV risk at Hard SL" cap and **no** "6% total SL-risk" cap.

- **Margin per coin: 2–4% of NAV**, with a hard cap of 4%.
  - This replaces P 4–8%, P+N 8–12%, P+N+CR 10–15% and Continuation 2–4%. The old BTC-bearish
    "fixed 4%" rule is subsumed by the 4% cap.
  - Margin = Claude's `size_pct` clamped to [2%, 4%]. If Claude gives no size, the 4% cap is used.
- **Leverage 3–5x isolated** and never above the coin's HL maxLeverage. A coin with maxLeverage
  below 3x is skipped. Claude's `leverage` is a maximum inside 3–5x.
- **Liquidation must sit beyond the Hard SL.** For a LONG, the isolated liquidation price from the
  worst-case entry (IOC limit) must be strictly below the tier Hard SL.
  - Try leverage from the maximum allowed (min of 5x, coin max and AI max) down to 3x, and use the
    first one that passes.
  - If even 3x fails, the coin is **skipped** and the reason is reported.
- The existing **80% total margin cap** is unchanged.
- **ADD_ON keeps the existing position's leverage.** It is fixed and not stepped, even if the
  existing leverage is below 3x (e.g. MON at 2x). The +10% ROE and 5.5% NAV coin-margin gates
  above still apply.
- **Claude's values are maximums.** AI values below the floors are lifted to the floor (2% margin,
  3x) and noted in `sizing_notes`, because the legacy default of 2x can't comply with 3–5x.
  - If a lower AI value should mean "skip", that needs a new MMT decision.
- Unchanged:
  - SL distance ≥ 1.5%
  - minimum order max($10, 1% NAV)
  - price sanity
  - radar row-count
  - the Base above-Upper guard
  - all exits and Hard SL placement
- The cockpit/AI candidate suggestions (`suggested_size_pct` / `suggested_leverage`, desk
  `sot2`) use the same sizing on the 1D close.
- Open positions are **not** resized. MON stays as is.
- Code: `exec_common.size_by_margin` and `exec_common.addon_gates`.

---

## GIIQ-SoT-1 (2026-09-28)

This is the first versioned SoT. It collects every executor change made on 2026-09-28 (HKT).

### Execution plumbing (PR #17)
- **Railway key agent check.** The executor no longer compares the key against a hardcoded
  API-wallet address. Before trading, it checks that the key's address is an *approved,
  unexpired agent* of the main wallet (HL `extraAgents`). The current key is 0x3eC8…eb72,
  named "Railway Key". `HL_API_WALLET_ADDRESS` is informational only.
- **Preflight** (`exec_preflight.py`). Runs at boot and at 08:45 HKT, never trades. It checks:
  - mode: `EXEC_DRY_RUN` and whether the key is present
  - the agent check above, with an expiry warning
  - the account
  - a signed probe (cancel of the non-existent oid 1)
  - today's approvals, leverage feasibility and the entry-guard preview
  - pending entries

  Results are available at keyed `GET/POST /api/exec/preflight`. A failure shows a red banner in
  the cockpit.
- **Keyed re-run** at `POST /api/exec/run`. Same executor, same env, same fail-closed checks.
- A problem with the signing client is a loud executor **error** (cockpit banner), not a silent
  per-coin skip.

### At-entry Upper guard (PR #18, refined in #19)
- **Base** entries (fresh 1D dual-cross-up; a row that is both Base and Chase counts as Base)
  enter at 08:55 HKT only if a live HL mid, fetched right before the order, is **above the 1D
  Upper** of the latest closed-bar scan. Otherwise the coin is skipped and an alert is shown.

### Pending pullback entries for Chase approvals (PR #19, preflight labels in #20)
- An approved **Chase never enters at 08:55.** The executor (LIVE only) creates a pending record
  in `out/pending_entries.json`:
  - **ADD_ON**: the coin is already held LONG. Band TF 4H, zone [4H Lower, 4H Filter].
  - **CONTINUATION**: no position. Band TF 1D, zone [1D Lower, 1D Filter].
- **N / N+1 confirmation**, run in the 4H :10 HKT job after the scan and exits:
  - Bar N is a closed band-TF bar with low ≤ Filter and close > Lower.
  - Bar N+1 must close > Lower and > bar N's close, with trend Green. The entry is then placed at
    the live mid.
- **Cancelled** on any band-TF close below Lower, after 30 days (was 7; MMT 2026-10-07), when a CONTINUATION coin is
  already held, or when an ADD_ON's base position has closed.
- All fail-closed SoT checks run again at fill time:
  - radar freshness
  - liquidation safety of open positions
  - tier Hard SL, with SL distance ≥ 1.5%
  - size band 2–4% (ADD_ON used the same band; superseded by GIIQ-SoT-2)
  - leverage 1–5x and ≤ the coin's maxLeverage
  - minimum order
  - 80% margin cap
  - isolated liquidation beyond the Hard SL
- **Known caveat:** Small/Tiny Hard SL = 4H Filter. ADD_ON fills (and some CONTINUATION fills)
  are therefore usually blocked by the 1.5% SL-distance check and expire.

### Cockpit display (PR #19)
- The header badge shows the **real** exec mode from the service env: LIVE in red, DRY_RUN in
  amber.
- The ENTRY tab shows the AI decision for each candidate: APPROVED (Chase: "→ pending pullback")
  or VETO, with size, leverage and reason.
- A blue pending bar shows zone, bar-N state, mid and expiry.

### Guardrails from the Signum reference (this PR)
These are guardrails only. Entry/exit triggers, sizing bands and leverage rules are unchanged, and
no top-up / ROE rule was added.

1. **Radar row-count check** (`exec_common.radar_rowcount_ok`).
   - Rows on 2026-09-28: 1D 173, 4H 176, 1H 177. That is the whole HL liquid universe
     (dayNtlVlm ≥ $75k, about 160–180 names).
   - The executor and the pending worker **fail closed** (no entries, no pending state change)
     when the 1D or 4H closed-bar radar has **< 120 rows** (`RADAR_MIN_ROWS`, about 70% of
     normal).
   - They also fail closed when a radar has **< 85% of its `universe_requested`**
     (`RADAR_MIN_UNIVERSE_FRAC`), i.e. more than 15% of the requested coins failed to load.
   - Env overrides: `EXEC_RADAR_MIN_ROWS`, `EXEC_RADAR_MIN_UNIVERSE_FRAC`. A value of 0 disables
     the check and is meant for tests only.
   - The preflight shows `radar_rows:1d/4h/1h`.
   - Exits are **not** blocked by this check. The on-exchange Hard SL always protects positions.
2. **Price sanity.**
   - A coin is skipped when the HL live mid differs from the radar price by **> 50%** (ticker
     collision or bad data).
   - Radar price = latest closed 4H close, falling back to the 1D close, then to the 1D close
     frozen in the candidate.
   - A missing price also means skip.
   - Applies to executor Base entries and to pending fills.
3. **Minimum order.** An entry is skipped when its notional is < **max(HL minimum $10, 1% of
   NAV)**.
4. **NAV snapshot, taken once per run** (`exec_common.nav_snapshot`) and used for every
   size / margin-cap / minimum-order check in that run.
   - **Unified account** (HL `userAbstraction` = `unifiedAccount`, our main wallet): NAV = spot
     USDC `total`. Perp `accountValue` (isolated margin + uPnL) is already inside it as `hold`, so
     it is not added again.
   - **Standard account:** NAV = perp `accountValue` + spot USDC `total`.
   - **Unknown mode:** NAV = max(spot USDC total, perp accountValue).
   - Non-USDC spot tokens are not counted.
   - **Order: exits → re-fetch positions → entries.** The 4H :10 job runs the exit worker first.
     Pending entries run only if the exit step completed OK (otherwise they are skipped and the
     job status says why), and the pending worker re-fetches positions after the exits. The 08:55
     executor and `/api/exec/run` do entries only.
5. **Daily run report.**
   - Every executor and pending run produces a `run_report` listing **executed / skipped /
     downsized / failed** entries with reasons.
     - *downsized*: the final size % or leverage is below the AI-approved value because of an SoT
       clamp.
     - *failed*: a LIVE order was attempted but did not execute.
   - Reports are stored per HKT day in `out/run_reports/YYYY-MM-DD.json`.
   - They are shown in the cockpit "Run report" panel and included in `/api/desk-data` and
     `[DESK_DATA]` (`run_report`), together with `sot`, `exec_mode` and `pending_entries`.
6. **SoT id** `GIIQ-SoT-1` and this changelog.

Reference: `docs/reference/signum_workflow_reference.md`. It is reference only: Signum is never
used for orders.

## BX liquidity tiers (2026-10-07, MMT) — SoT id unchanged (BX universe rule, not an executor rule)
- `bx_universe.VOL_TRADEABLE` $2M -> **$200K** (tradeable = 24h vol >= $200K and spread <= 10 bp); `VOL_WATCH_MIN` $300K -> $200K.
- Spread limit unchanged at 10 bp. Spread depth is now measured for every contract with vol >= $200K.
- `bx_live`: candidates and orders on coins under $1M 24h volume carry the flag `low_vol_under_1M` (never blocks).
- `LIQ_EXIT_VOL` $1M -> $200K (MMT): `liquidity_exit` for open BX trades now only below $200K or spread > 30 bp.

## Pending CONT / ADD_ON expiry 7 -> 30 days; manual BX run; naming (2026-10-07, MMT)
- `pending_entries.PENDING_TTL_DAYS` 7 -> **30** (also the `PENDING_STALE` health check). Still cancelled on a 1D close below 1D Lower.
- New manual route `POST /api/bx/run` (cockpit, X-AI-Key; forwarded to bx-exec): body `{"confirm": true, "symbols": ["BRUSDT"]}`.
  Runs the normal 08:56 entry checks for the listed symbols only, at most once per HKT day per symbol.
- Naming: the signal formerly called **"Chase"** is a **4H Breakout** signal (1D Green + 4H Green + 4H close crossing up through the 4H Upper).
  It is a signal, not an order. An approved 4H Breakout becomes a **CONT** (coin not held) or **ADD_ON** (coin held) pending.
  Internal data values (`type: "Chase"` in the candidates / decisions API) are unchanged so the desk and Railway keep working.

## CONT dropped, ADD_ON = Signum top-up (2026-10-07, MMT) — OFF by default
- No CONT: a coin you do not hold enters only through the Base (fresh daily cross + Green). The 4H "Chase" CONT / ADD_ON pullback
  pendings stay disabled (`PENDING_CONTINUATION_DISABLED`, default on) and are not used.
- New `daily_addon.py` (08:57 HKT, after the 08:55 executor; `DAILY_ADDON_ENABLED=1` to switch on, DRY_RUN unless live): for each coin held LONG,
  add 2% NAV margin at the position's existing isolated leverage when the latest CLOSED 1D bar's close is still above the 1D Upper,
  the position's PRICE gain is >= +10% and the coin's notional after the add stays <= 20% NAV. Once per coin per HKT day.
  Same fail-closed checks as every entry (radar row-count + freshness, liq beyond Hard SL, Hard SL per tier, SL distance, price sanity,
  min order, 80% total margin). Mechanical (no desk decision), like Signum.
- Manual dry-run: cockpit job `addon`. Tests: `test_daily_addon.py`.
