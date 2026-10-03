# Entry Approval Gate

This document explains the **rule-based entry candidate list** system and AI approval/veto workflow for Own Trend Radar.

## Overview

The entry approval process is **fail-closed**: no entry orders are placed unless explicitly approved by an AI operator before the daily market open (09:30 HKT).

## Entry Candidate Rules

Railway generates a daily entry candidate list (`entry_candidates_YYYYMMDD.json` and `entry_candidates_latest.json`) based on **deterministic rules** applied to closed bars from the 1D and 4H radars:

### Base Entry
- **Trigger**: 1D Upper dual_cross_up
  - `close > upper` AND `prev_close <= prev_upper`
- **Trend**: Green or Red (both OK)
- **Universe**: Symbols already in radar (passing `dayNtlVlm >= $75k` gate)

### Chase Entry
- **Trigger**: All three conditions:
  1. 1D trend = Green
  2. 4H trend = Green
  3. 4H Upper dual_cross_up (`close > upper` AND `prev_close <= prev_upper`)
- **Universe**: Same as Base

### Output Fields (per candidate)
- `symbol`: Asset ticker
- `type`: "Base" or "Chase"
- `tier`: Market cap tier (from `mcap_tiers.py`)
- `trend_1d`, `trend_4h`: Trend colors
- `close_1d`, `upper_1d`, `filter_1d`: 1D GC levels
- `filter_4h`: 4H Filter (Hard SL level for all tiers)
- `hard_sl_dist_pct`: Distance from close to 4H Filter as percentage
- `dayNtlVlm`: Daily notional volume
- `already_held`: Boolean flag if position exists

### Top-Level Metadata
- `generated_at`: UTC ISO timestamp
- `radar_1d_asof`, `radar_4h_asof`: Radar scan timestamps
- `radar_1d_age_h`, `radar_4h_age_h`: Radar ages in hours
- `stale`: True if 1D > 36h or 4H > 2h
- `btc`: BTC trend_1d, trend_4h, close_vs_filter_1d
- `count`: Number of candidates
- `candidates[]`: List of candidate objects

## AI Approval Workflow

1. **Primary**: Harbor AI reviews the candidate list and may **VETO** any or all candidates.
2. **Backup**: Grok.ai (external) can fetch the list via the read-only endpoint and provide a **VETO** if Harbor is unavailable.
3. **Deadline**: Approval/veto must be completed by **09:30 HKT** (01:30 UTC).
4. **Fail-Closed**: If no approval by deadline, **no entry orders are placed**.

### AI Veto Only
The AI may only **remove** candidates from the list. The AI **cannot add** symbols not already flagged by the rules.

## Read-Only Endpoint for Backup AI

**Endpoint**: `GET /api/entry-candidates?key=<ENTRY_READ_KEY>`

### Authentication
- `X-AI-Key` header (preferred) or query parameter `key` compared with environment variable `ENTRY_READ_KEY` using constant-time `hmac.compare_digest`.
- If `ENTRY_READ_KEY` unset: endpoint returns **404** (disabled).
- If `key` missing or invalid: returns **403** (forbidden).
- If `key` matches: returns **200** with JSON.

### Domain
The backup AI (Grok.ai) should fetch from the Railway production domain:

```
https://cockpit-production-ec2c.up.railway.app/api/entry-candidates?key=<KEY>
```

**Do NOT use** the custom domain `giiqquant.com` for this endpoint.

### Security
- This endpoint **bypasses the cockpit password gate** ONLY for this route.
- It is **read-only** and exposes no other data (no full radar, no positions).
- The response is compact JSON containing only the entry candidate list.

## Generation Triggers

The candidate list is regenerated:
1. After the daily **1D radar scan** (~08:05 HKT / 00:05 UTC).
2. After a **POST /api/sync** that writes `out/gc_radar_1d.json`.

Exceptions are **non-fatal** and logged in one line (stderr).

## Hard SL (Stop Loss)

As of 2026-09-21, the **Hard SL** is unified across all tiers:
- **Level**: 4H Filter (mid, period 72)
- **Output**: `filter_4h` field in each candidate
- **Distance**: `hard_sl_dist_pct = (close_1d - filter_4h) / close_1d * 100`

## Example JSON

```json
{
  "generated_at": "2026-09-25T00:15:00+00:00",
  "radar_1d_asof": "2026-09-25T00:05:00Z",
  "radar_4h_asof": "2026-09-25T00:00:00Z",
  "radar_1d_age_h": 0.17,
  "radar_4h_age_h": 0.25,
  "stale": false,
  "btc": {
    "trend_1d": "Green",
    "trend_4h": "Green",
    "close_vs_filter_1d": 2.34
  },
  "count": 2,
  "candidates": [
    {
      "symbol": "SOL",
      "type": "Chase",
      "tier": "large",
      "trend_1d": "Green",
      "trend_4h": "Green",
      "close_1d": 150.5,
      "upper_1d": 148.2,
      "filter_1d": 145.0,
      "filter_4h": 147.0,
      "hard_sl_dist_pct": 2.33,
      "dayNtlVlm": 250000,
      "already_held": false
    },
    {
      "symbol": "ATOM",
      "type": "Base",
      "tier": "small",
      "trend_1d": "Green",
      "trend_4h": "Red",
      "close_1d": 10.5,
      "upper_1d": 10.2,
      "filter_1d": 9.8,
      "filter_4h": 10.0,
      "hard_sl_dist_pct": 4.76,
      "dayNtlVlm": 100000,
      "already_held": false
    }
  ]
}
```

## Operational Notes

- **No LLM** is used to generate the candidate list. It is purely rule-based.
- The candidate list is **deterministic** and **reproducible** from the radar data.
- The list is **written to disk** with HKT-dated filenames for audit trail.
- The endpoint is **designed for external AI access** (e.g., Grok.ai via xAI) as a fail-safe backup to Harbor.

---

**Last Updated**: 2026-09-25
