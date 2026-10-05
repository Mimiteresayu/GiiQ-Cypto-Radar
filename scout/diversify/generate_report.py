#!/usr/bin/env python3
"""Generate diversify_latest.md from backtest results v2"""

import json
import sys
from datetime import datetime


def main():
    # Try both absolute and relative paths
    paths = [
        'scout/diversify/out/backtest_results_v2.json',
        'out/backtest_results_v2.json'
    ]
    
    results = None
    for path in paths:
        try:
            with open(path, 'r') as f:
                results = json.load(f)
            break
        except FileNotFoundError:
            continue
    
    if results is None:
        print("ERROR: backtest_results_v2.json not found in any expected location", file=sys.stderr)
        sys.exit(1)
    
    meta = results.get('meta', {})
    strategies = results.get('strategies', {})
    
    lines = [
        "# GiiQ Crypto Diversification: Backtest v2 Results",
        "",
        f"**Generated:** {meta.get('timestamp', 'N/A')}  ",
        f"**Version:** {meta.get('version', 'N/A')}  ",
        f"**Earliest Data:** {meta.get('earliest_data', 'N/A')}  ",
        f"**Avg Days per Coin:** {meta.get('avg_days', 'N/A')}  ",
        "",
        "---",
        "",
        "## Sanity Test: Buy & Hold BTC",
        ""
    ]
    
    sanity = meta.get('sanity_test', {})
    if sanity.get('pass'):
        lines.append(f"✅ **PASS** (error: {sanity.get('error', 'N/A')})")
    else:
        lines.append(f"❌ **FAIL** (error: {sanity.get('error', 'N/A')})")
    
    lines.extend([
        f"- Calculated return: {sanity.get('calculated_return', 'N/A')}",
        f"- Expected return: {sanity.get('expected_return', 'N/A')}",
        f"- Days: {sanity.get('days', 'N/A')}",
        "",
        "---",
        "",
        "## Strategy Comparison",
        "",
        "| Strategy | Total Ret | CAGR | Sharpe | MDD | PF | Trades | Underpowered | Corr GiiQ BO | Corr BTC |",
        "|----------|-----------|------|--------|-----|----|----|--------------|-------------|----------|"
    ])
    
    for strat_key, strat_data in strategies.items():
        if isinstance(strat_data, dict) and 'error' in strat_data:
            lines.append(f"| {strat_key} | ERROR | {strat_data['error'][:30]} | - | - | - | - | - | - | - |")
        elif isinstance(strat_data, dict):
            under = "⚠️ <50" if strat_data.get('underpowered', False) else "✅"
            corrs = strat_data.get('correlations', {})
            lines.append(
                f"| {strat_key} | "
                f"{strat_data.get('total_return', 'N/A')} | "
                f"{strat_data.get('cagr', 'N/A')} | "
                f"{strat_data.get('sharpe', 'N/A')} | "
                f"{strat_data.get('max_drawdown', 'N/A')} | "
                f"{strat_data.get('profit_factor', 'N/A')} | "
                f"{strat_data.get('num_trades', 0)} | "
                f"{under} | "
                f"{corrs.get('giiq_bo', 'N/A')} | "
                f"{corrs.get('btc', 'N/A')} |"
            )
    
    lines.extend([
        "",
        "---",
        "",
        "## IS/OOS Breakdown",
        "",
        "| Strategy | IS Return (≤2023) | IS Sharpe | OOS Return (2024+) | OOS Sharpe |",
        "|----------|------------------|-----------|-------------------|------------|"
    ])
    
    for strat_key, strat_data in strategies.items():
        if isinstance(strat_data, dict) and 'error' not in strat_data:
            is_sum = strat_data.get('is_summary', {})
            oos_sum = strat_data.get('oos_summary', {})
            lines.append(
                f"| {strat_key} | "
                f"{is_sum.get('return', 'N/A')} | "
                f"{is_sum.get('sharpe', 'N/A')} | "
                f"{oos_sum.get('return', 'N/A')} | "
                f"{oos_sum.get('sharpe', 'N/A')} |"
            )
    
    lines.extend([
        "",
        "---",
        "",
        "## Strategy Details",
        ""
    ])
    
    for strat_key, strat_data in strategies.items():
        if isinstance(strat_data, dict) and 'error' in strat_data:
            lines.extend([
                f"### {strat_key}",
                "",
                f"**ERROR:** {strat_data['error']}",
                ""
            ])
            continue
        
        if not isinstance(strat_data, dict):
            continue
        
        lines.extend([
            f"### {strat_key}",
            "",
            f"**Strategy:** {strat_data.get('strategy', 'N/A')}  ",
            f"**Total Return:** {strat_data.get('total_return', 'N/A')}  ",
            f"**CAGR:** {strat_data.get('cagr', 'N/A')}  ",
            f"**Sharpe:** {strat_data.get('sharpe', 'N/A')}  ",
            f"**Max Drawdown:** {strat_data.get('max_drawdown', 'N/A')}  ",
            f"**Profit Factor:** {strat_data.get('profit_factor', 'N/A')}  ",
            f"**Trades:** {strat_data.get('num_trades', 0)}  ",
            f"**Days:** {strat_data.get('num_days', 0)}  ",
            ""
        ])
        
        if strat_data.get('underpowered'):
            lines.extend([
                "⚠️ **UNDERPOWERED (<50 trades)** - Results are statistically unreliable.",
                ""
            ])
        
        if 'per_pair' in strat_data:
            lines.extend([
                "#### Per-Pair Trade Counts",
                ""
            ])
            for pair, counts in strat_data['per_pair'].items():
                lines.append(f"- **{pair}**: {counts.get('entries', 0)} entries, {counts.get('exits', 0)} exits")
            lines.append("")
        
        yearly = strat_data.get('yearly_summary', {})
        if yearly:
            lines.extend([
                "#### Year-by-Year Performance",
                "",
                "| Year | Return | Sharpe | MDD | Trades |",
                "|------|--------|--------|-----|--------|"
            ])
            for year in sorted(yearly.keys()):
                y = yearly[year]
                lines.append(
                    f"| {year} | {y.get('return', 'N/A')} | "
                    f"{y.get('sharpe', 'N/A')} | {y.get('mdd', 'N/A')} | "
                    f"{y.get('trades', 0)} |"
                )
            lines.append("")
        
        detailed = strat_data.get('detailed_trades', [])
        if detailed:
            lines.extend([
                "#### Sample Trades (first 10)",
                "",
                "| Entry Date | Exit Date | Pair/Assets | Direction | Entry Z/Signal | Exit Z/Signal | PnL Gross | PnL Net | Reason |",
                "|------------|-----------|-------------|-----------|----------------|--------------|-----------|---------|--------|"
            ])
            for t in detailed[:10]:
                if 'pair' in t:
                    lines.append(
                        f"| {t.get('entry_date', 'N/A')} | {t.get('exit_date', 'N/A')} | "
                        f"{t.get('pair', 'N/A')} | {t.get('direction', 'N/A')} | "
                        f"{t.get('entry_z', 'N/A')} | {t.get('exit_z', 'N/A')} | "
                        f"{t.get('gross_pnl_pct', 'N/A')}% | {t.get('net_pnl_pct', 'N/A')}% | "
                        f"{t.get('reason', 'N/A')} |"
                    )
                else:
                    date = t.get('date', 'N/A')
                    long_c = t.get('long_coins', 'N/A')
                    short_c = t.get('short_coins', 'N/A')
                    turn = t.get('turnover', 'N/A')
                    cost = t.get('cost_pct', 'N/A')
                    lines.append(
                        f"| {date} | - | Long: {long_c}, Short: {short_c} | Rebalance | "
                        f"- | - | - | - | Turnover: {turn}, Cost: {cost} |"
                    )
            lines.append("")
    
    lines.extend([
        "---",
        "",
        "## Specification Summary",
        "",
        "1. **Cross-Sectional Reversal**: Weekly rebalance, point-in-time top-50 perps by trailing 30d volume, long bottom quintile (0.5 weight) / short top quintile (0.5 weight) of 7-day return, 7-day hold.",
        "",
        "2. **Funding Cross-Section**: Weekly, long lowest-funding quintile / short highest-funding quintile (market-neutral), 7-day hold, paginated HL `fundingHistory`. ⚠️ Insufficient historical data (HL API limitation).",
        "",
        "3. **Pairs Mean Reversion**: BTC/ETH, BTC/SOL, ETH/SOL, SOL/ARB log-spread z-score (30d lookback, enter |z|>2, exit z=0, stop |z|>4), 25% capital per pair (12.5% per leg, dollar neutral, 1:1 log hedge ratio, short spread when z>2, long spread when z<-2).",
        "",
        "4. **GiiQ BO Proxy**: Long-only 20-day Donchian breakout, exit at 10-day low.",
        "",
        "5. **Costs**: Taker 0.045% + slippage 0.05% per side per leg when applicable.",
        "",
        "6. **Data**: Hyperliquid Public API `candleSnapshot` (up to 5000 candles per coin, startTime: 0).",
        "",
        "---",
        "",
        f"*Report generated {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}*"
    ])
    
    report = '\n'.join(lines)
    
    # Try both paths
    output_paths = ['scout/diversify/out/diversify_latest.md', 'out/diversify_latest.md']
    output_path = None
    
    for path in output_paths:
        try:
            import os
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'w') as f:
                f.write(report)
            output_path = path
            break
        except:
            continue
    
    print(f"Report generated: {output_path}")


if __name__ == '__main__':
    main()
