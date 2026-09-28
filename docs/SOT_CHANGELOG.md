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

## GIIQ-SoT-2 (2026-09-28): sizing / leverage / ADD_ON rule change (approved by MMT)

Entry and exit **triggers are unchanged**. These rules apply to Base entries (08:55 executor and
`/api/exec/run`) and to pending fills (ADD_ON / CONTINUATION, 4H :10 job). Code:
`exec_common.size_by_risk`, `exec_common.addon_gates`, `exec_common.open_risk_usd`.

### 1. ADD_ON pending fills: extra gates at fill time
- The existing position's **ROE must be ≥ +10%**. ROE = HL `returnOnEquity`, falling back to
  uPnL ÷ marginUsed.
- The coin's **total margin after the add must be ≤ 5.5% of NAV** (per-run NAV snapshot):
  existing `marginUsed` + new margin ≤ 5.5%.
  - The new margin is capped by the room left under 5.5%.
  - If the room is below the 2% minimum margin, the add is skipped.
- A failed gate means no order. The record stays pending, and the reason appears in the daily
  run report (`skipped`).

### 2. Leverage 3–5x isolated, chosen by risk (replaces the flat 2x and the old size bands)
- **Margin per coin: 2–4% of NAV**, with a hard cap of 4%. This replaces P 4–8%, P+N 8–12%,
  P+N+CR 10–15% and Continuation 2–4%. The old BTC-bearish "fixed 4%" rule is subsumed by the 4%
  cap.
- **Leverage 3–5x isolated** and never above the coin's HL maxLeverage. A coin with maxLeverage
  below 3x is skipped.
- **Risk at Hard SL** = notional × SL distance, where SL distance is measured from the worst-case
  entry (IOC limit). Risk must be **≤ 1.5% NAV per trade, with a target of 1%**.
- **Total open risk ≤ 6% NAV.** For open LONGs this is size × (mid − tier Hard SL). New entries in
  the same run count cumulatively.
- **Liquidation buffer:** the isolated liquidation price must sit below the Hard SL by at least
  **2× the SL distance**, i.e. (Hard SL − liq) ≥ 2 × (entry − Hard SL).
- **Algorithm:** try leverage from the maximum allowed (5x, coin max, AI max) down to 3x.
  - Skip a leverage step if its liquidation buffer fails.
  - Margin = the margin that targets 1% risk, clamped to [2%, cap].
  - Margin is then reduced so that risk ≤ 1.5% and total open risk ≤ 6%.
  - Accept the first leverage whose margin is still ≥ 2%.
  - If it is impossible even at 3x / 2% margin, the coin is **skipped** and the reason is
    reported.
- **ADD_ON keeps the existing position's leverage.** It is fixed and not stepped, even if the
  existing leverage is below 3x (e.g. MON at 2x).
- **Claude's `size_pct` / `leverage` are maximums**, clamped by these rules. AI values below the
  floors are lifted to the floor (2% margin, 3x) and noted in `sizing_notes`, because the legacy
  default of 2x can't comply with 3–5x.
  - If a lower AI value should mean "skip", that needs a new MMT decision.
- Unchanged:
  - SL distance ≥ 1.5%
  - 80% total margin cap
  - minimum order max($10, 1% NAV)
  - price sanity
  - radar row-count
  - the Base above-Upper guard
  - all exits and Hard SL placement
- The cockpit/AI candidate suggestions (`suggested_size_pct` / `suggested_leverage`, desk
  `sot2`) use the same sizing on the 1D close.
- Open positions are **not** resized. MON stays as is.

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
- **Cancelled** on any band-TF close below Lower, after 7 days, when a CONTINUATION coin is
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
