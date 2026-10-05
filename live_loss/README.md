# live_loss — weekly live-loss diagnosis (read-only)

## Purpose
Every Monday 08:30 HKT, diagnose last ISO week's closed HL trades (by close time, HKT) against the Logic Card rules **that were live at each trade's entry**, and give each losing trade one cause:
`OFF_SOT` → `RULE_BREACH` → `HARD_SL_HIT` → `COST_DOMINATED` → `FAST_FAIL` (<24h) → `TIER_EXIT_NORMAL` → `UNEXPLAINED`.
Also: KPIs (net, PF, win rate, fees, funding), SoT losing-streak p-value vs backtest win rate, CARD_GAP tags, and a completeness check (every HL close order in the window must be in the trade log).
No orders, no exchange keys. Only public HL info API reads (`portfolio` for point-in-time NAV, `userFillsByTime` for the check).
Spec / owners: Prism draft, Forge builds + maintains, Cove owns classification rules (plan: Prism `live_loss/AUTOMATION_PLAN.md`).

## Inputs
| Input | Env / flag | Where it comes from |
|---|---|---|
| Trade log CSV (path or URL) | `LIVE_LOSS_TRADES` / `--trades` | **Not in this repo.** Forge wires it (see below). |
| Optional bearer for the URL | `LIVE_LOSS_TRADES_TOKEN` | GitHub secret; never printed |
| HL wallet (public) | `LIVE_LOSS_WALLET` / `--wallet` | main wallet; enables point-in-time NAV + completeness check |
| mcap tiers | `LIVE_LOSS_MCAP` | `data/mcap_cache.json` (this repo) |
| Backtest win rate | `LIVE_LOSS_BT` | `live_loss/bt_ref.json` |

Trade-log columns required: `time_open_hkt, time_close_hkt, symbol, side, entry, exit, size, notional_usd, leverage, fees, pnl_usd (net of fees+funding), exit_reason, source`.
`source` must contain `SoT_auto_railway` or `pre_SoT`, `exit oid <ids>`, `gross_closedPnl=<x>`, `funding=<x>` (same format as Forge's current `hl_live_trades.csv` exporter).

### What Forge must wire (before Mon 2026-10-12 08:30 HKT)
1. Publish the trade log somewhere the Action can GET: either the read-only cockpit export (`GET /api/exec/exits` → CSV in the columns above, read-only key in secret `LIVE_LOSS_TRADES_TOKEN`), or Forge's exporter writing the CSV to a private location (e.g. private gist / Brain) at a fixed URL.
2. Set repo secret `LIVE_LOSS_TRADES_URL` (secret only; a repo variable is not read).
3. Outputs: workflow **artifact** only (90 days). The workflow has `contents: read` and never commits; `live_loss/out/` is git-ignored and the diagnosis is written to `out/run.log` (artifact), not the job log, because this repo is **public**.
4. Run once via *Actions → live_loss weekly → Run workflow* with `week=2026-W40` and compare with Prism's box output (5 SoT trades, −30.21, 0 breaches).

## Re-run
```bash
python live_loss/diagnose.py --trades hl_live_trades.csv --wallet 0xcFCd...B122 --week 2026-W40   # local
python live_loss/diagnose.py --all                                                                   # whole history
```
Idempotent: overwrites `live_loss/out/<YYYY-Www>/{diagnosis.md,diagnosis.json,losing_trades.csv}`. `conclusions.md` is written by the bot reading these, never by the job.
When the Logic Card changes, add a row to `RULES` (name, effective deploy time HKT, limits) and bump `CARD`.

## Common faults
| Symptom | Cause / fix |
|---|---|
| `LIVE_LOSS_TRADES_URL not set` | Secret not wired (step 2). |
| `trade log fetch failed: HTTPError 401/403` | Token missing/rotated → update `LIVE_LOSS_TRADES_TOKEN`. |
| `trade log missing columns [...]` | Exporter changed format → fix exporter or column map. |
| exit 2, "trade log incomplete: N HL close order(s) not in log" | Exporter lagging/broken; rerun after export. Trades are NOT silently ignored. |
| "point-in-time NAV unavailable" / "HL completeness check failed" | HL API 429/outage; script retries 4×. Rerun via workflow_dispatch. |
| Many `RULE_BREACH` right after a deploy | `RULES` table missing the new rule version/effective time. |
| `UNEXPLAINED` causes | `exit_reason` empty (Hard SL/liquidation/manual not in cockpit trade_log). |
