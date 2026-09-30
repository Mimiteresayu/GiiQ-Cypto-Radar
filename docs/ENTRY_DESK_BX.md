# ENTRY_DESK — Bitunix (BX) section (add to the 08:10 HKT desk task after Harbor approves)

Proposal for Harbor review; not active until MMT pastes it into the desk task.

## Input
`GET /api/ai/candidates?key=…` now has a separate `bx` object next to the HL candidates:
`bx.candidates[]` = today's pilot-eligible Bitunix signals (already filtered: BX-only crypto, 24h vol ≥ $2M,
spread < 10 bp, 1D or 4H GC). Fields: `symbol` (e.g. FOOUSDT), `coin`, `type` Base | Chase | NewToken, `gc_tf`,
`close`, `upper_1d`, `lower_1d`, `filter_1d`, `trend_1d`, `upper_4h`, `filter_4h`, `lower_4h`, `breakout_4h_pct`,
`last_1d_cross_days`, `hard_sl`, `sl_dist_pct`, `vol24h_usd`, `spread_bp`, `ign_x`, `narrative`, `cat_tags`,
`max_leverage`, `asset_age`. If `bx.error` is set or the list is empty: post no BX decisions.

## Rules (same ids as HL)
- **V1_WEAK_4H_BREAKOUT** — Chase / NewToken with `breakout_4h_pct` < 0.5 → veto.
- **V2_OLD_CROSS** (interim) — Chase (CONTINUATION) with `last_1d_cross_days` > 7 → veto.
- **V3_BELOW_UPPER** — Base with `close` < `upper_1d` → veto. **V3_BELOW_LOWER** — Chase with `close` < `lower_1d` → veto.
- **V5_NO_N_NO_V** — not `narrative` and `vol24h_usd` < $2M → veto (rare: the list already needs ≥ $2M).
- V7_JUDGMENT / V8_DATA as for HL.
- **Approve at most 1 BX candidate.** Size and leverage are fixed (1% NAV, 3x): send no size_pct / leverage.
- Grok, news and dimension scores may only veto.

## Output
Add `bx_decisions` to the same POST as the HL decisions (or post it alone):
```
POST /api/ai/decision  (X-AI-Key)
{"source": "claude",
 "decisions": [ …HL as today… ],
 "bx_decisions": [
   {"coin": "FOO", "action": "APPROVE", "type": "BASE", "reason": "…"},
   {"coin": "BAR", "action": "VETO", "type": "CHASE", "rule": "V1_WEAK_4H_BREAKOUT", "reason": "…"}]}
```
Deadline 08:50 HKT (entries run 08:56). There is **no fallback** for BX: no approval means no order.
The response carries `bx: {ok, stored, rejected, late}`; a symbol not in today's BX list is rejected.
