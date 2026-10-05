#!/usr/bin/env python3
"""Generate markdown summary report from search and backtest results."""

import json
import sys
from datetime import datetime


def load_json(path):
    """Load JSON file."""
    try:
        with open(path, 'r') as f:
            return json.load(f)
    except Exception as e:
        print(f"Error loading {path}: {e}", file=sys.stderr)
        return None


def generate_report(search_results, backtest_results, output_path):
    """Generate markdown report."""
    
    lines = []
    lines.append("# GiiQ Diversification Strategy Report")
    lines.append("")
    lines.append(f"**Generated:** {datetime.now().astimezone().strftime('%Y-%m-%d %H:%M %Z')}")
    lines.append("")
    lines.append("**Objective:** Identify 1–2 non-trend crypto strategies with low correlation to GiiQ BO (daily channel-breakout trend strategy).")
    lines.append("")
    
    # Data limitations
    if backtest_results and 'meta' in backtest_results:
        meta = backtest_results['meta']
        lines.append("## Data Limitations")
        lines.append("")
        lines.append(f"- **Source:** {meta.get('data_source', 'Unknown')}")
        lines.append(f"- **Lookback:** {meta.get('lookback_days', 'Unknown')} days")
        lines.append(f"- **Universe:** {meta.get('universe_size', 'Unknown')} liquid perpetuals")
        lines.append(f"- **Limitations:** {meta.get('data_limitations', 'None noted')}")
        lines.append("")
    
    # Backtest results
    lines.append("## Backtest Results")
    lines.append("")
    
    if backtest_results and 'strategies' in backtest_results:
        strategies = backtest_results['strategies']
        
        # Summary table
        lines.append("| Strategy | PF | CAGR | Sharpe | MDD | Trades | Corr(GiiQ BO) | Corr(BTC) | IS Return | OOS Return |")
        lines.append("|----------|-----|------|--------|-----|--------|---------------|-----------|-----------|------------|")
        
        strategy_names = {
            'cross_sectional_reversal': '1. Cross-Sectional Reversal',
            'funding_cross_section': '2. Funding Cross-Section',
            'pairs_mean_reversion': '3. Pairs Mean Reversion'
        }
        
        for key, name in strategy_names.items():
            if key in strategies:
                s = strategies[key]
                if 'error' in s:
                    lines.append(f"| {name} | ERROR | - | - | - | - | - | - | - | - |")
                else:
                    underpowered = ' ⚠️ UNDERPOWERED' if s.get('underpowered', False) else ''
                    corr_giiq = s.get('correlations', {}).get('giiq_bo', 'N/A')
                    corr_btc = s.get('correlations', {}).get('btc', 'N/A')
                    is_ret = s.get('is_summary', {}).get('return', 'N/A')
                    oos_ret = s.get('oos_summary', {}).get('return', 'N/A')
                    lines.append(f"| {name}{underpowered} | {s.get('profit_factor', 'N/A')} | {s.get('cagr', 'N/A')} | {s.get('sharpe', 'N/A')} | {s.get('max_drawdown', 'N/A')} | {s.get('num_trades', 0)} | {corr_giiq} | {corr_btc} | {is_ret} | {oos_ret} |")
        
        lines.append("")
        lines.append("**Costs:** 0.045% taker fee + 0.05% slippage per side; funding included for strategy 2.")
        lines.append("")
        
        # Detailed results for each strategy
        for key, name in strategy_names.items():
            if key in strategies:
                s = strategies[key]
                lines.append(f"### {name}")
                lines.append("")
                
                if 'error' in s:
                    lines.append(f"**Error:** {s['error']}")
                    lines.append("")
                    continue
                
                # Spec
                if key == 'cross_sectional_reversal':
                    lines.append("**Spec:** Weekly rebalance, long bottom quintile / short top quintile of 7-day return, top-50 liquid perps, 7-day hold.")
                elif key == 'funding_cross_section':
                    lines.append("**Spec:** Weekly rebalance, long lowest-funding / short highest-funding quintile (30-day avg), market-neutral, 7-day hold.")
                elif key == 'pairs_mean_reversion':
                    lines.append("**Spec:** BTC/ETH, BTC/SOL, ETH/SOL, SOL/ARB pairs, 30-day z-score lookback, enter |z|>2, exit z=0, stop |z|>4.")
                
                lines.append("")
                
                # Metrics
                lines.append(f"- **Total Return:** {s.get('total_return', 'N/A')}")
                lines.append(f"- **CAGR:** {s.get('cagr', 'N/A')}")
                lines.append(f"- **Sharpe Ratio:** {s.get('sharpe', 'N/A')}")
                lines.append(f"- **Max Drawdown:** {s.get('max_drawdown', 'N/A')}")
                lines.append(f"- **Profit Factor:** {s.get('profit_factor', 'N/A')}")
                lines.append(f"- **Number of Trades:** {s.get('num_trades', 0)}")
                
                if s.get('underpowered', False):
                    lines.append(f"- **⚠️ UNDERPOWERED:** <50 trades, results may not be statistically significant")
                
                lines.append(f"- **Days Traded:** {s.get('num_days', 0)}")
                lines.append("")
                
                # Correlations
                corr = s.get('correlations', {})
                lines.append("**Correlations:**")
                lines.append("")
                lines.append(f"- vs GiiQ BO (20d Donchian): {corr.get('giiq_bo', 'N/A')}")
                lines.append(f"- vs BTC: {corr.get('btc', 'N/A')}")
                lines.append("")
                
                # Year-by-year table
                if 'yearly_summary' in s and s['yearly_summary']:
                    lines.append("**Year-by-Year Performance:**")
                    lines.append("")
                    lines.append("| Year | Return | Sharpe | MDD | Trades |")
                    lines.append("|------|--------|--------|-----|--------|")
                    for year, data in sorted(s['yearly_summary'].items()):
                        lines.append(f"| {year} | {data.get('return', 'N/A')} | {data.get('sharpe', 'N/A')} | {data.get('mdd', 'N/A')} | {data.get('trades', 0)} |")
                    lines.append("")
                
                # IS/OOS
                lines.append("**In-Sample (≤2023) vs Out-of-Sample (2024-2026):**")
                lines.append("")
                is_summary = s.get('is_summary', {})
                oos_summary = s.get('oos_summary', {})
                lines.append(f"- **IS:** Return {is_summary.get('return', 'N/A')}, Sharpe {is_summary.get('sharpe', 'N/A')}, {is_summary.get('days', 0)} days")
                lines.append(f"- **OOS:** Return {oos_summary.get('return', 'N/A')}, Sharpe {oos_summary.get('sharpe', 'N/A')}, {oos_summary.get('days', 0)} days")
                lines.append("")
                
                # Per-pair for pairs strategy
                if key == 'pairs_mean_reversion' and 'per_pair' in s:
                    lines.append("**Per-Pair Breakdown:**")
                    lines.append("")
                    lines.append("| Pair | Trades | Entries | Exits |")
                    lines.append("|------|--------|---------|-------|")
                    for pair, data in sorted(s['per_pair'].items()):
                        lines.append(f"| {pair} | {data.get('trades', 0)} | {data.get('entries', 0)} | {data.get('exits', 0)} |")
                    lines.append("")
    
    else:
        lines.append("**No backtest results available.**")
        lines.append("")
    
    # Search results
    lines.append("## Strategy Discovery")
    lines.append("")
    
    if search_results:
        # GitHub
        if 'github' in search_results and search_results['github']:
            lines.append("### Top GitHub Repositories")
            lines.append("")
            
            for i, repo in enumerate(search_results['github'][:10], 1):
                lines.append(f"**{i}. [{repo['repo']}]({repo['url']})** ({repo['stars']} ⭐)")
                lines.append(f"   - Last push: {repo['last_push']}")
                
                backtest_status = '✓ Backtest + metrics' if repo.get('has_metrics') else ('✓ Backtest mentioned' if repo.get('has_backtest_mention') else '✗ No backtest')
                lines.append(f"   - {backtest_status}")
                
                if repo.get('description'):
                    lines.append(f"   - {repo['description'][:150]}")
                
                lines.append("")
        
        # arXiv
        if 'arxiv' in search_results and search_results['arxiv']:
            lines.append("### arXiv Papers")
            lines.append("")
            
            for paper in search_results['arxiv'][:5]:
                lines.append(f"- **[{paper['title']}]({paper['url']})**")
                lines.append(f"  - Published: {paper['published']} | ID: {paper['id']}")
                if paper.get('summary'):
                    lines.append(f"  - {paper['summary'][:150]}...")
                lines.append("")
        
        # SSRN
        if 'ssrn' in search_results and search_results['ssrn']:
            lines.append("### SSRN Papers")
            lines.append("")
            
            for paper in search_results['ssrn']:
                lines.append(f"- [{paper['title']}]({paper['url']})")
                if paper.get('note'):
                    lines.append(f"  - {paper['note']}")
                lines.append("")
    
    else:
        lines.append("**No search results available.**")
        lines.append("")
    
    # Footer
    lines.append("---")
    lines.append("")
    lines.append("**Next Steps:**")
    lines.append("")
    lines.append("1. Review top strategies for low correlation with GiiQ BO")
    lines.append("2. Extend backtests with Binance data archive for longer history + delisted coins")
    lines.append("3. Compute actual correlation with GiiQ BO daily returns")
    lines.append("4. Select 1–2 candidates for Railway deployment")
    lines.append("")
    
    # Write report
    report_text = '\n'.join(lines)
    
    with open(output_path, 'w') as f:
        f.write(report_text)
    
    print(f"Report generated: {output_path}")
    return report_text


def main():
    import os
    
    # Use absolute paths or paths relative to the script
    script_dir = os.path.dirname(os.path.abspath(__file__))
    out_dir = os.path.join(script_dir, 'out')
    
    # Create out directory if it doesn't exist
    os.makedirs(out_dir, exist_ok=True)
    
    search_path = os.path.join(out_dir, 'search_results.json')
    backtest_path = os.path.join(out_dir, 'backtest_results.json')
    output_path = os.path.join(out_dir, 'diversify_latest.md')
    
    search_results = load_json(search_path)
    backtest_results = load_json(backtest_path)
    
    report = generate_report(search_results, backtest_results, output_path)
    
    print("\n" + "=" * 80)
    print("REPORT PREVIEW")
    print("=" * 80)
    print(report[:1000])
    print("...")


if __name__ == '__main__':
    main()
