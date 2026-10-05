# Scout gate-1 screen (v1, 2026-10-04 — PF aligned to Prism's unified method)

## screen.py

`python3 screen.py <0x address> [--days 90] [--slip 0.0005] [--json] [--raw DIR]`. Works on an HL vault or wallet. Uses the public HL info API only, no keys. Runtime is about 3 min for a ~5k-fill vault and about 7–10 min for a long-history vault like Growi (funding is paged over the full history, with a 1 s pause between pages to avoid HL 429s). `--raw DIR` replays `fills.json` + `funding.json` dumps offline. Market and equity data still come from the live API in that mode.

**Reports:**
- Beta vs BTC and vs a top-20 equal-weight basket (top 20 by HL volume), each with R², plus beta share of PnL.
- Time-weighted net-long share, reconstructed from fills.
- MDD over allTime (from HL weekly points, so understated) and within the `--days` window.

These use the `--days` window.

**PF (method from `prism/jump69_reconcile/report.md` + `PREREG.md`):**
- **Data:** the full fill history. `userFillsByTime` (aggregateByTime) is paged forward from t=0 with no cap and de-duplicated. `userFunding` is paged the same way over the full history.
- **Coverage:** the report always shows completed-round-trip fills / total fills as a percentage (and counts).
- **Round trip:** per coin, from flat to flat. A flip counts as one close plus one open. The flip fill's fee and slippage are split |startPosition|/sz to the closing trip and the rest to the new trip, and its closedPnl goes to the closing trip. Positions still open at the end are left out (listed as a NOTE). Spot fills are ignored.
- **Per-trip net:** closedPnl − fees + funding over the holding window (that coin's `userFunding` records with open < t ≤ close) − route costs.
- **PF = Σ winning nets / |Σ losing nets|.** Costs are never added to the denominator.
- **Copy route (GATE):** fee = max(actual fee, 0.045% × notional), plus `--slip` (0.05%) × notional per side on every fill. No profit share.
- **Vault-deposit route (reference only):** actual fees + funding, no extra slippage. 10% profit share on net profit above the HWM, computed on the aggregate as in Prism's PREREG: PF_V = (W − 0.1·max(W−L, 0)) / L.
- **Last `--days` PF** (trips closed inside the window, both routes) is a recency view only, never the gate.

**Gate-1 FAIL if any of these hold:**
- Beta share >50% with R² ≥0.3.
- Net long >80% of the time.
- **Copy-route full-history PF ≤1.2.** n/a also fails, because the position is never flat or because completed-round-trip coverage is below 50%.
- **Completed-round-trip PF coverage <50% of all fills.** Copy PF is reported as n/a and this is a FAIL. Coverage is always shown as completed-round-trip fills / total fills.
- allTime MDD >33%.

Plus manual Cove criterion 9: reproducible on Railway, no third-party keys.

PASS means hand to Cove, who runs IS/OOS and the month-block permutation test. There is no "check with Cove" band any more.

NOTE lines (not fails) flag:
- Fill history truncated by the HL API (funding records start more than 2 days before the first served fill).
- Open positions left out of PF.

**Validation (live runs 2026-10-04; outputs in `validation/`):**

| | Copy PF full (gate) | Vault PF full (ref) | PF coverage (completed / total fills) | Copy / Vault last 90d | Trips | Other | Verdict |
|---|---|---|---|---|---|---|---|
| 69 Jump St `0xa844…e802` | **1.19** (1.1917) | 1.29 | **100.0% (5,403 / 5,403)** | 1.72 / 1.75 | 658 (2025-01-19 → 2026-10-02) | beta share 166% but R² 0.10 (no flag); net long 63%; MDD 9.4% | **FAIL** (PF ≤1.2) |
| Growi HF `0x1e37…8d5e` | **n/a** (raw 17.80) | 23.97 | **5.5% (742 / 13,370)** | n/a / 23.97 | 40, all kSHIB | **beta share 100% (R² 0.61); net long 90%; coverage <50%**; MDD 12.3% | **FAIL** (beta, net long, PF coverage) |

- **Jump vs Prism:** these match Prism's Route C 1.1917, Route V 1.289, and 90d 1.719 / 1.755 on the same 5,403 fills.
  - The 4th-decimal difference (1.19174 vs 1.19166) comes from one choice. This script splits the extra copy fee on a flip fill between the two trips. Prism adds the whole flip fill's extra fee to both trips.
  - The old v0 formula (90d, costs in the denominator, 10% PS hack) gave 1.52, and 1.15 on the full history.
- **Growi:** HL serves fills only from 2026-08-04 (13,370 fills), but funding goes back to 2024-07-08. Its 19-coin book is never flat, so its PF is meaningless; the 5.5% coverage now independently makes copy PF n/a and fails the gate. It also fails on beta and net long.

**Limits:**
- HL's API may not serve very old fills on high-frequency accounts. Check the truncation NOTE when it appears.
- The equity series is sparse (weekly points).
- Unrealized PnL of open positions is not in PF.
- Non-HL bots (e.g. from GitHub) need their own backtest. The screen covers HL addresses only.

## discover.py

`python3 discover.py [--top-n 10]`. Automated candidate discovery from public, no-key sources. Uses the public HL API, Binance/Bybit/OKX public funding endpoints, and GitHub search API (with the built-in `GITHUB_TOKEN` in Actions). Runtime is about 5–15 minutes depending on the number of vaults to screen.

**Sources:**
1. **HL vaults:** from `https://stats-data.hyperliquid.xyz/Mainnet/vaults`
   - Excludes HLP protocol/system vaults that can't be copied or reproduced: HLP parent vault (0xdfc2...f303), any vault name starting with 'HLP' (e.g. HLP Liquidator 1-4), and any vault whose leader matches the HLP leader
   - Hard filters: TVL ≥ $100k, age ≥ 90 days, 90-day return > 0, MDD < 33% (from available portfolio/equity data; note it is weekly-ish and understated)
   - Sorts by TVL (largest first) to ensure biggest vaults get screened first within the time budget
   - Runs `screen.py` on top N (default 20, configurable via `--top-n`, ~900s timeout per vault)
   - Adds HTTP 429 backoff (exponential: 2, 4, 8, 16s) for HL rate limits
   - SUSPECT flag: copy PF >10 or MDD <1% with <30 round trips (needs manual check, shown separately from PASS)
   - Sparse MDD: if allTime history has <10 points, reports MDD as n/a with a NOTE instead of 0%
   - Skips anything in `rejected.json`

2. **Funding spreads:** HL `metaAndAssetCtxs` plus Binance, Bybit, OKX public funding endpoints
   - Lists coins whose annualized funding difference across venues is ≥ 15%
   - Also lists coins with HL 30-day average funding > 10% APR (team rule: standalone carry only worth re-checking above that)
   - Handles geo-blocks gracefully: logs the venue as unavailable and continues, never fails the whole job

3. **Open-source bots:** GitHub search API using the built-in `GITHUB_TOKEN`
   - Keywords: `funding arbitrage`, `liquidation`, `order flow`, `market making`, `hyperliquid`
   - Filters: stars ≥ 200, pushed within 90 days
   - Lists: repo, stars, last push, whether README mentions backtest results
   - Marks as 'needs manual criterion 9 (reproducible on Railway, no third-party keys)'

**Outputs:**
- `out/YYYY-MM-DD.json`: full data (all candidates, screen results, funding table, bot list, errors)
- `out/latest.md`: ≤ 1 page summary
  - PASS list first with key numbers (copy PF, beta share, net long %, MDD)
  - SUSPECT list (needs manual check): copy PF >10 or MDD <1% with <30 round trips
  - Funding table (coin, HL 30d avg, spread vs Binance/Bybit/OKX; falls back to HL predictedFundings when exchanges are geo-blocked)
  - Bot list (repo, stars, last push, backtest results)
  - Counts of what was cut and why (e.g. "15 vaults failed TVL filter, 8 failed age filter, 3 failed MDD filter, 2 already rejected, 5 HLP excluded")
  - "No data" explanation: vaults missing required fields (TVL, age, return history) or closed vaults
  - Source errors (e.g. "Binance, Bybit, OKX geo-blocked (using HL predictedFundings)")

## GitHub Actions Workflow

`.github/workflows/scout_screen.yml`

- **Schedule:** cron `30 1 * * 1-5` (09:30 HKT weekdays)
- **Manual trigger:** `workflow_dispatch`
- **PR trigger:** runs on pull requests that touch `scout/**` or `.github/workflows/scout_screen.yml`
- **Timeout:** 20 minutes
- **Environment:** Python 3.11, only stdlib + `requests` (installed in the job, not via repo requirements.txt)
- **On schedule/dispatch:** commits `scout/out/` back to the `scout-out` branch with `GITHUB_TOKEN` (permissions: `contents: write`, `issues: write`) using a commit message containing `[skip ci]`
- **On pull_request:** just uploads the outputs as an artifact (no commit)
- **On failure (schedule/dispatch):** opens a GitHub issue titled `scout screen failed <date>`
- **No secrets, no exchange keys, no orders**

**Rationale for `scout-out` branch:** Daily commits to `main` would trigger Railway redeploys of the live services (`cockpit`, `bx-exec`). To avoid this, the workflow pushes daily outputs to a separate branch `scout-out` instead. The PR against `main` will include the initial workflow setup, but all automated commits go to `scout-out`.

## rejected.json

Seed list of known-rejected candidates. The discovery script skips anything in this list.

Current rejections:
- 69 Jump Street `0xa844d7ac9fa3424c4fd38a25baa23e460ec3e802`: copy-route full-history PF 1.19 ≤1.2
- Growi HF `0x1e37a337ed460039d1b15bd3bc489de789768d5e`: beta share 100% (R² 0.61); net long 90%; PF coverage <50%
- PF1 `0xa1b6d8efbcb2fb750a84dbc05649fa4968034f04`: manual criterion 9 (not reproducible on Railway)

## Workflow Integration

**10:20 routine (LLM):** reads `out/latest.md` from the `scout-out` branch. If no candidates pass gate 1, one sentence to Cove. If any pass, review each (max 2 hours per candidate), then hand to Cove by 19:00 HKT.

**Cove:** runs IS/OOS and the month-block permutation test on candidates that pass gate 1.

**Prism:** validates PF calculations against Prism's unified method.

## Token Savings

**Before (manual discovery + screening):** ~1.0–1.5M tokens per run (measured baseline from ~28 tool rounds on 10/5/2026)
**After (automated discovery + screening):** ≤25k tokens per run (only reads 1-page summary + reviews ≤3 candidates)
**Savings:** ~98% per run, ~22M–33M tokens per month

## Cost

GitHub Actions: private repos get 2,000 free minutes/month. Each run is ~5 minutes × 22 weekdays ≈ 110 minutes/month, so **$0**.
