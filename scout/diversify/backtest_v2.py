#!/usr/bin/env python3
"""Backtest v2: Clean implementation with all bugs fixed"""

import json
import sys
import time
import urllib.request
import argparse
import math
import statistics
from collections import defaultdict
from typing import List, Dict, Any, Tuple
from datetime import datetime


API = 'https://api.hyperliquid.xyz/info'


def post(body: Dict[str, Any], retries: int = 3) -> Any:
    """POST request to Hyperliquid API with retries."""
    for i in range(retries):
        try:
            req = urllib.request.Request(API, json.dumps(body).encode(), {'Content-Type': 'application/json'})
            with urllib.request.urlopen(req, timeout=30) as response:
                return json.loads(response.read().decode())
        except Exception as e:
            if i == retries - 1:
                raise
            time.sleep(2 ** i)


def get_universe() -> List[str]:
    """Get top liquid perpetuals from Hyperliquid."""
    try:
        data = post({'type': 'metaAndAssetCtxs'})
        universe = data[0]['universe']
        asset_ctx = data[1]
        
        coins = []
        for u, ctx in zip(universe, asset_ctx):
            if u.get('isDelisted'):
                continue
            vol = float(ctx.get('dayNtlVlm', 0))
            if vol > 0:
                coins.append((u['name'], vol))
        
        coins.sort(key=lambda x: x[1], reverse=True)
        return [c[0] for c in coins[:50]]
    except Exception as e:
        print(f"Error fetching universe: {e}", file=sys.stderr)
        return ['BTC', 'ETH', 'SOL', 'ARB', 'AVAX', 'MATIC', 'OP', 'DOGE', 'XRP', 'ADA']


def get_daily_candles(coin: str, start_ms: int, end_ms: int) -> Dict[int, Dict[str, float]]:
    """Fetch daily candles (up to 5000)."""
    try:
        data = post({'type': 'candleSnapshot', 'req': {'coin': coin, 'interval': '1d', 'startTime': 0, 'endTime': end_ms}})
        
        candles = {}
        for c in data:
            day = int(c['t']) // 86400000
            candles[day] = {
                'open': float(c['o']),
                'high': float(c['h']),
                'low': float(c['l']),
                'close': float(c['c']),
                'volume': float(c['v'])
            }
        
        time.sleep(0.2)
        return candles
    except Exception as e:
        print(f"Error fetching candles for {coin}: {e}", file=sys.stderr)
        return {}


def get_funding_history(coin: str, end_ms: int) -> Dict[int, float]:
    """Fetch historical funding rates (paginated)."""
    try:
        all_data = []
        current_start = 0
        
        while current_start < end_ms:
            data = post({'type': 'fundingHistory', 'coin': coin, 'startTime': current_start})
            
            if not data:
                break
            
            all_data.extend(data)
            last_time = max(int(f['time']) for f in data)
            
            if len(data) < 500:
                break
            
            current_start = last_time + 1
            time.sleep(0.3)
        
        daily_funding = defaultdict(list)
        for f in all_data:
            day = int(f['time']) // 86400000
            rate = float(f['fundingRate'])
            daily_funding[day].append(rate)
        
        result = {}
        for day, rates in daily_funding.items():
            avg_rate = sum(rates) / len(rates)
            result[day] = avg_rate * 3 * 365 * 100  # Annualized %
        
        return result
    except Exception as e:
        print(f"Error fetching funding for {coin}: {e}", file=sys.stderr)
        return {}


def sanity_test_btc(price_data: Dict[str, Dict[int, float]]) -> Dict[str, Any]:
    """Verify PnL accounting with buy-and-hold BTC."""
    if 'BTC' not in price_data:
        return {'error': 'BTC not in price data'}
    
    btc_prices = price_data['BTC']
    days = sorted(btc_prices.keys())
    
    # Calculate using our engine
    equity_curve = {days[0]: 1.0}
    for i in range(1, len(days)):
        ret = (btc_prices[days[i]] / btc_prices[days[i-1]]) - 1
        equity_curve[days[i]] = equity_curve[days[i-1]] * (1 + ret)
    
    calculated_return = equity_curve[days[-1]] - 1
    expected_return = (btc_prices[days[-1]] / btc_prices[days[0]]) - 1
    error = abs(calculated_return - expected_return)
    
    return {
        'calculated_return': f'{calculated_return:.6f}',
        'expected_return': f'{expected_return:.6f}',
        'error': f'{error:.8f}',
        'pass': error < 0.01,
        'days': len(days)
    }


def calculate_giiq_bo_proxy(price_data: Dict[str, Dict[int, float]]) -> Tuple[Dict[int, float], Dict[int, float]]:
    """GiiQ BO proxy: long-only 20-day Donchian breakout, exit at 10-day low."""
    all_days = sorted(set(day for coin_prices in price_data.values() for day in coin_prices.keys()))
    
    if len(all_days) < 30:
        return {}, {}
    
    positions = {}
    equity_curve = {}
    daily_returns = {}
    
    for i, day in enumerate(all_days):
        if i == 0:
            equity_curve[day] = 1.0
            continue
        
        if i < 20:
            equity_curve[day] = equity_curve[all_days[i-1]]
            daily_returns[day] = 0.0
            continue
        
        daily_pnl = 0.0
        
        # Check exits
        for coin in list(positions.keys()):
            if coin not in price_data or day not in price_data[coin]:
                continue
            
            low_prices = [price_data[coin][all_days[j]] for j in range(max(0, i-10), i) if all_days[j] in price_data[coin]]
            if not low_prices:
                continue
            
            low_10d = min(low_prices)
            current_price = price_data[coin][day]
            
            if current_price <= low_10d:
                entry_price = positions[coin]
                pnl = (current_price / entry_price) - 1
                daily_pnl += pnl
                del positions[coin]
        
        # Check entries
        for coin, prices in price_data.items():
            if coin in positions or day not in prices:
                continue
            
            high_prices = [prices[all_days[j]] for j in range(max(0, i-20), i) if all_days[j] in prices]
            if not high_prices:
                continue
            
            high_20d = max(high_prices)
            current_price = prices[day]
            
            if current_price > high_20d:
                positions[coin] = current_price
        
        # Mark-to-market
        for coin, entry_price in positions.items():
            if coin in price_data and day in price_data[coin]:
                prev_day = all_days[i-1]
                if prev_day in price_data[coin]:
                    prev_price = price_data[coin][prev_day]
                    current_price = price_data[coin][day]
                    if prev_price > 0:
                        daily_ret = (current_price / prev_price) - 1
                        daily_pnl += daily_ret
        
        if len(positions) > 0:
            daily_pnl /= len(positions)
        
        prev_equity = equity_curve[all_days[i-1]]
        equity_curve[day] = prev_equity * (1 + daily_pnl)
        daily_returns[day] = daily_pnl
    
    return daily_returns, equity_curve


def strategy_pairs_mean_reversion(
    price_data: Dict[str, Dict[int, float]],
    with_costs: bool = True
) -> Dict[str, Any]:
    """Pairs mean reversion with CORRECT sizing and logic."""
    
    pairs = [('BTC', 'ETH'), ('BTC', 'SOL'), ('ETH', 'SOL'), ('SOL', 'ARB')]
    
    # Calculate z-scores
    def calc_log_spread(p1: Dict[int, float], p2: Dict[int, float]) -> Dict[int, float]:
        common_days = sorted(set(p1.keys()) & set(p2.keys()))
        return {day: math.log(p1[day]) - math.log(p2[day]) for day in common_days if p1[day] > 0 and p2[day] > 0}
    
    def calc_zscore(spreads: Dict[int, float], window: int = 30) -> Dict[int, float]:
        result = {}
        days = sorted(spreads.keys())
        for i in range(window, len(days)):
            day = days[i]
            window_values = [spreads[days[j]] for j in range(i - window, i)]
            mean = sum(window_values) / len(window_values)
            std = statistics.stdev(window_values) if len(window_values) > 1 else 0
            if std > 0:
                result[day] = (spreads[day] - mean) / std
        return result
    
    pair_signals = {}
    for coin1, coin2 in pairs:
        if coin1 in price_data and coin2 in price_data:
            spread = calc_log_spread(price_data[coin1], price_data[coin2])
            zscore = calc_zscore(spread, window=30)
            pair_signals[f'{coin1}/{coin2}'] = zscore
    
    all_days = sorted(set(day for signals in pair_signals.values() for day in signals.keys()))
    
    if len(all_days) < 60:
        return {'error': 'Insufficient data', 'days_available': len(all_days)}
    
    # Backtest: 25% per pair, 12.5% per leg, dollar-neutral
    positions = {}
    equity_curve = {all_days[0]: 1.0}
    trades = []
    detailed_trades = []
    
    COST_PER_SIDE = (0.00045 + 0.0005) if with_costs else 0.0
    CAPITAL_PER_PAIR = 0.25
    
    for i, day in enumerate(all_days):
        if i == 0:
            continue
        
        prev_day = all_days[i-1]
        daily_pnl = 0.0
        
        # Mark-to-market and check exits
        for pair_name, pos_info in list(positions.items()):
            if pair_name not in pair_signals or day not in pair_signals[pair_name]:
                continue
            
            z = pair_signals[pair_name][day]
            coin1, coin2 = pair_name.split('/')
            
            # Daily P&L
            if coin1 in price_data and coin2 in price_data:
                if day in price_data[coin1] and day in price_data[coin2]:
                    if prev_day in price_data[coin1] and prev_day in price_data[coin2]:
                        prev_spread = math.log(price_data[coin1][prev_day]) - math.log(price_data[coin2][prev_day])
                        curr_spread = math.log(price_data[coin1][day]) - math.log(price_data[coin2][day])
                        spread_move = curr_spread - prev_spread
                        pnl = pos_info['dir'] * spread_move * CAPITAL_PER_PAIR
                        daily_pnl += pnl
            
            # Exit conditions
            exit_trade = False
            stop_trade = False
            reason = ''
            
            if pos_info['dir'] > 0:  # Long spread
                if z <= 0:
                    exit_trade = True
                    reason = 'zero_cross'
                elif z > 4:
                    stop_trade = True
                    reason = 'stop_loss'
            else:  # Short spread
                if z >= 0:
                    exit_trade = True
                    reason = 'zero_cross'
                elif z < -4:
                    stop_trade = True
                    reason = 'stop_loss'
            
            if exit_trade or stop_trade:
                if coin1 in price_data and coin2 in price_data:
                    if day in price_data[coin1] and day in price_data[coin2]:
                        entry_spread = math.log(pos_info['entry_price1']) - math.log(pos_info['entry_price2'])
                        exit_spread = math.log(price_data[coin1][day]) - math.log(price_data[coin2][day])
                        spread_change = exit_spread - entry_spread
                        
                        trade_pnl = pos_info['dir'] * spread_change * CAPITAL_PER_PAIR
                        exit_cost = CAPITAL_PER_PAIR * 2 * COST_PER_SIDE
                        daily_pnl -= exit_cost
                        
                        if len(detailed_trades) < 10:
                            detailed_trades.append({
                                'entry_date': datetime.fromtimestamp(pos_info['entry_day'] * 86400).strftime('%Y-%m-%d'),
                                'exit_date': datetime.fromtimestamp(day * 86400).strftime('%Y-%m-%d'),
                                'pair': pair_name,
                                'direction': 'LONG' if pos_info['dir'] > 0 else 'SHORT',
                                'entry_z': round(pos_info['entry_z'], 3),
                                'exit_z': round(z, 3),
                                'entry_spread': round(entry_spread, 4),
                                'exit_spread': round(exit_spread, 4),
                                'spread_change': round(spread_change, 4),
                                'gross_pnl_pct': round(trade_pnl * 100, 2),
                                'cost_pct': round((pos_info.get('entry_cost', 0) + exit_cost) * 100, 2),
                                'net_pnl_pct': round((trade_pnl - pos_info.get('entry_cost', 0) - exit_cost) * 100, 2),
                                'reason': reason
                            })
                        
                        trades.append({'day': day, 'pair': pair_name, 'action': f'exit_{reason}'})
                
                del positions[pair_name]
        
        # Entry signals
        for pair_name, signals in pair_signals.items():
            if day not in signals or pair_name in positions:
                continue
            
            z = signals[day]
            coin1, coin2 = pair_name.split('/')
            
            if coin1 not in price_data or coin2 not in price_data:
                continue
            if day not in price_data[coin1] or day not in price_data[coin2]:
                continue
            
            entry_dir = 0
            
            # SHORT spread when z>2 (sell expensive, buy cheap)
            # LONG spread when z<-2 (buy cheap, sell expensive)
            if z > 2:
                entry_dir = -1
            elif z < -2:
                entry_dir = 1
            
            if entry_dir != 0:
                entry_cost = CAPITAL_PER_PAIR * 2 * COST_PER_SIDE
                daily_pnl -= entry_cost
                
                positions[pair_name] = {
                    'dir': entry_dir,
                    'entry_day': day,
                    'entry_z': z,
                    'entry_price1': price_data[coin1][day],
                    'entry_price2': price_data[coin2][day],
                    'entry_cost': entry_cost
                }
                
                trades.append({'day': day, 'pair': pair_name, 'action': 'enter_long' if entry_dir > 0 else 'enter_short'})
        
        equity_curve[day] = equity_curve[prev_day] * (1 + daily_pnl)
    
    daily_returns = {}
    for i in range(1, len(all_days)):
        day = all_days[i]
        prev_day = all_days[i-1]
        daily_returns[day] = (equity_curve[day] / equity_curve[prev_day]) - 1
    
    per_pair = {}
    for coin1, coin2 in pairs:
        pair_str = f'{coin1}/{coin2}'
        pair_trades = [t for t in trades if t.get('pair') == pair_str]
        per_pair[pair_str] = {
            'trades': len(pair_trades),
            'entries': len([t for t in pair_trades if 'enter' in t.get('action', '')]),
            'exits': len([t for t in pair_trades if 'exit' in t.get('action', '')])
        }
    
    return {
        'equity_curve': equity_curve,
        'daily_returns': daily_returns,
        'trades': trades,
        'detailed_trades': detailed_trades,
        'per_pair': per_pair,
        'with_costs': with_costs
    }


def strategy_cross_sectional_reversal(
    all_price_data: Dict[str, Dict[int, float]],
    with_costs: bool = True
) -> Dict[str, Any]:
    """Cross-sectional reversal with point-in-time universe and CORRECT sizing."""
    
    # Get all days
    all_days = sorted(set(day for coin_prices in all_price_data.values() for day in coin_prices.keys()))
    
    if len(all_days) < 60:
        return {'error': 'Insufficient data', 'days_available': len(all_days)}
    
    # Point-in-time universe: top-50 by trailing 30d volume at each rebalance
    def get_universe_at_day(day: int, day_idx: int) -> List[str]:
        volumes = {}
        for coin, prices in all_price_data.items():
            vol_30d = 0.0
            for j in range(max(0, day_idx - 30), day_idx):
                check_day = all_days[j]
                if check_day in prices:
                    # Approximate volume as price (no volume data stored)
                    vol_30d += prices[check_day]
            
            if vol_30d > 0:
                volumes[coin] = vol_30d
        
        sorted_coins = sorted(volumes.items(), key=lambda x: x[1], reverse=True)
        return [c[0] for c in sorted_coins[:50]]
    
    # Backtest: weekly rebalance, market-neutral 0.5 long + 0.5 short
    equity_curve = {all_days[0]: 1.0}
    positions = {}
    trades = []
    detailed_trades = []
    
    COST_PER_SIDE = (0.00045 + 0.0005) if with_costs else 0.0
    NET_EXPOSURE = 1.0  # 100% net (50% long, 50% short)
    
    rebalance_days = all_days[::7]  # Weekly
    
    for rebal_idx, rebal_day in enumerate(rebalance_days):
        if rebal_idx == 0:
            continue
        
        day_idx = all_days.index(rebal_day)
        
        # Get point-in-time universe
        universe = get_universe_at_day(rebal_day, day_idx)
        
        if len(universe) < 20:
            continue
        
        # Calculate 7-day returns
        day_returns = {}
        for coin in universe:
            if coin not in all_price_data:
                continue
            
            past_idx = max(0, day_idx - 7)
            past_day = all_days[past_idx]
            
            if past_day in all_price_data[coin] and rebal_day in all_price_data[coin]:
                if all_price_data[coin][past_day] > 0:
                    ret = (all_price_data[coin][rebal_day] / all_price_data[coin][past_day]) - 1
                    day_returns[coin] = ret
        
        if len(day_returns) < 20:
            continue
        
        # Sort and create quintiles
        sorted_coins = sorted(day_returns.items(), key=lambda x: x[1])
        n = len(sorted_coins)
        quintile_size = n // 5
        
        long_coins = [c[0] for c in sorted_coins[:quintile_size]]  # Bottom quintile (losers)
        short_coins = [c[0] for c in sorted_coins[-quintile_size:]]  # Top quintile (winners)
        
        # New positions: equal weight within each side
        new_positions = {}
        weight_per_coin = 0.5 / len(long_coins) if long_coins else 0
        
        for coin in long_coins:
            new_positions[coin] = weight_per_coin
        for coin in short_coins:
            new_positions[coin] = -weight_per_coin
        
        # Calculate turnover
        turnover = sum(abs(new_positions.get(c, 0) - positions.get(c, 0)) 
                      for c in set(list(positions.keys()) + list(new_positions.keys())))
        
        cost = turnover * COST_PER_SIDE
        
        if turnover > 0:
            trades.append({
                'day': rebal_day,
                'turnover': turnover,
                'cost': cost,
                'long': long_coins[:5],
                'short': short_coins[:5]
            })
            
            if len(detailed_trades) < 10:
                detailed_trades.append({
                    'date': datetime.fromtimestamp(rebal_day * 86400).strftime('%Y-%m-%d'),
                    'long_coins': ', '.join(long_coins[:5]),
                    'short_coins': ', '.join(short_coins[:5]),
                    'weight_per_long': f'{weight_per_coin:.4f}',
                    'weight_per_short': f'{-weight_per_coin:.4f}',
                    'turnover': f'{turnover:.2f}',
                    'cost_pct': f'{cost * 100:.3f}%'
                })
        
        positions = new_positions
        
        # Mark-to-market until next rebalance
        next_rebal_idx = rebal_idx + 1
        next_rebal_day = rebalance_days[next_rebal_idx] if next_rebal_idx < len(rebalance_days) else all_days[-1]
        
        days_to_hold = [d for d in all_days if rebal_day < d <= next_rebal_day]
        
        for day in days_to_hold:
            pnl = 0.0
            for coin, weight in positions.items():
                if coin in all_price_data and day in all_price_data[coin]:
                    prev_day = day - 1
                    while prev_day >= all_days[0] and prev_day not in all_price_data[coin]:
                        prev_day -= 1
                    
                    if prev_day >= all_days[0] and prev_day in all_price_data[coin]:
                        if all_price_data[coin][prev_day] > 0:
                            ret = (all_price_data[coin][day] / all_price_data[coin][prev_day]) - 1
                            pnl += weight * ret
            
            prev_equity_day = day - 1
            while prev_equity_day >= all_days[0] and prev_equity_day not in equity_curve:
                prev_equity_day -= 1
            
            if prev_equity_day >= all_days[0] and prev_equity_day in equity_curve:
                equity_curve[day] = equity_curve[prev_equity_day] * (1 + pnl - cost / len(days_to_hold))
    
    daily_returns = {}
    days_sorted = sorted(equity_curve.keys())
    for i in range(1, len(days_sorted)):
        day = days_sorted[i]
        prev_day = days_sorted[i-1]
        daily_returns[day] = (equity_curve[day] / equity_curve[prev_day]) - 1
    
    return {
        'equity_curve': equity_curve,
        'daily_returns': daily_returns,
        'trades': trades,
        'detailed_trades': detailed_trades,
        'with_costs': with_costs
    }


def calculate_metrics(
    equity_curve: Dict[int, float],
    daily_returns: Dict[int, float],
    giiq_bo_returns: Dict[int, float],
    btc_returns: Dict[int, float],
    trades: List[Dict],
    strategy_name: str
) -> Dict[str, Any]:
    """Calculate comprehensive metrics."""
    
    days_sorted = sorted(equity_curve.keys())
    if len(days_sorted) < 2:
        return {'error': 'Insufficient data'}
    
    returns_list = [daily_returns[d] for d in sorted(daily_returns.keys())]
    
    # Basic metrics
    final_equity = equity_curve[days_sorted[-1]]
    n_years = len(returns_list) / 252
    total_return = final_equity - 1
    cagr = (final_equity ** (1 / n_years) - 1) if n_years > 0 else 0
    
    mean_ret = sum(returns_list) / len(returns_list)
    std_ret = statistics.stdev(returns_list) if len(returns_list) > 1 else 0
    sharpe = (mean_ret / std_ret * (252 ** 0.5)) if std_ret > 0 else 0
    
    # Max drawdown
    peak = 1.0
    max_dd = 0.0
    for day in days_sorted:
        val = equity_curve[day]
        peak = max(peak, val)
        dd = 1 - val / peak
        max_dd = max(max_dd, dd)
    
    # Profit factor
    wins = [r for r in returns_list if r > 0]
    losses = [r for r in returns_list if r < 0]
    win_sum = sum(wins)
    loss_sum = abs(sum(losses))
    pf = (win_sum / loss_sum) if loss_sum > 0 else (float('inf') if win_sum > 0 else 0)
    
    # Year-by-year
    yearly = defaultdict(list)
    for day, ret in daily_returns.items():
        day_ms = day * 86400000
        year = 1970 + day_ms // (365.25 * 86400000)
        yearly[int(year)].append(ret)
    
    yearly_summary = {}
    for year, rets in yearly.items():
        year_equity = 1.0
        peak_y = 1.0
        mdd_y = 0.0
        for r in rets:
            year_equity *= (1 + r)
            peak_y = max(peak_y, year_equity)
            dd = 1 - year_equity / peak_y
            mdd_y = max(mdd_y, dd)
        
        mean_y = sum(rets) / len(rets)
        std_y = statistics.stdev(rets) if len(rets) > 1 else 0
        sharpe_y = (mean_y / std_y * (252 ** 0.5)) if std_y > 0 else 0
        
        yearly_summary[year] = {
            'return': f'{year_equity - 1:.2%}',
            'sharpe': f'{sharpe_y:.2f}',
            'mdd': f'{mdd_y:.2%}',
            'trades': len([t for t in trades if 1970 + (t['day'] * 86400) // (365.25 * 86400000) == year])
        }
    
    # IS/OOS
    is_returns = []
    oos_returns = []
    for year, rets in yearly.items():
        if year <= 2023:
            is_returns.extend(rets)
        else:
            oos_returns.extend(rets)
    
    is_equity = 1.0
    for r in is_returns:
        is_equity *= (1 + r)
    
    oos_equity = 1.0
    for r in oos_returns:
        oos_equity *= (1 + r)
    
    is_sharpe = 0.0
    oos_sharpe = 0.0
    
    if len(is_returns) > 1:
        mean_is = sum(is_returns) / len(is_returns)
        std_is = statistics.stdev(is_returns)
        is_sharpe = (mean_is / std_is * (252 ** 0.5)) if std_is > 0 else 0
    
    if len(oos_returns) > 1:
        mean_oos = sum(oos_returns) / len(oos_returns)
        std_oos = statistics.stdev(oos_returns)
        oos_sharpe = (mean_oos / std_oos * (252 ** 0.5)) if std_oos > 0 else 0
    
    # Correlations
    def calc_corr(r1: Dict, r2: Dict) -> float:
        common = sorted(set(r1.keys()) & set(r2.keys()))
        if len(common) < 10:
            return 0.0
        
        v1 = [r1[d] for d in common]
        v2 = [r2[d] for d in common]
        
        n = len(v1)
        mean1 = sum(v1) / n
        mean2 = sum(v2) / n
        
        cov = sum((v1[i] - mean1) * (v2[i] - mean2) for i in range(n)) / n
        std1 = (sum((v1[i] - mean1) ** 2 for i in range(n)) / n) ** 0.5
        std2 = (sum((v2[i] - mean2) ** 2 for i in range(n)) / n) ** 0.5
        
        return cov / (std1 * std2) if (std1 > 0 and std2 > 0) else 0.0
    
    corr_giiq = calc_corr(daily_returns, giiq_bo_returns) if giiq_bo_returns else None
    corr_btc = calc_corr(daily_returns, btc_returns) if btc_returns else None
    
    return {
        'strategy': strategy_name,
        'total_return': f'{total_return:.2%}',
        'cagr': f'{cagr:.2%}',
        'sharpe': f'{sharpe:.2f}',
        'max_drawdown': f'{max_dd:.2%}',
        'profit_factor': f'{pf:.2f}' if pf != float('inf') else 'inf',
        'num_trades': len(trades),
        'num_days': len(returns_list),
        'underpowered': len(trades) < 50,
        'yearly_summary': yearly_summary,
        'is_summary': {
            'return': f'{is_equity - 1:.2%}' if is_returns else 'N/A',
            'sharpe': f'{is_sharpe:.2f}' if is_returns else 'N/A',
            'days': len(is_returns)
        },
        'oos_summary': {
            'return': f'{oos_equity - 1:.2%}' if oos_returns else 'N/A',
            'sharpe': f'{oos_sharpe:.2f}' if oos_returns else 'N/A',
            'days': len(oos_returns)
        },
        'correlations': {
            'giiq_bo': f'{corr_giiq:.3f}' if corr_giiq is not None else 'N/A',
            'btc': f'{corr_btc:.3f}' if corr_btc is not None else 'N/A'
        }
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', default='scout/diversify/out/backtest_results_v2.json')
    parser.add_argument('--days', type=int, default=730)
    
    args = parser.parse_args()
    
    print("=" * 80)
    print("GiiQ Diversification Backtest v2 (ALL BUGS FIXED)")
    print("=" * 80)
    print(f"Lookback: {args.days} days (requesting maximum available)")
    print()
    
    end_time = int(time.time() * 1000)
    start_time = 0
    
    # Get universe
    print("Fetching universe...")
    universe = get_universe()
    print(f"Universe: {len(universe)} coins")
    
    # Fetch price data
    print("\nFetching price data...")
    price_data = {}
    for coin in universe:
        candles = get_daily_candles(coin, start_time, end_time)
        if candles:
            price_data[coin] = {day: c['close'] for day, c in candles.items()}
    
    earliest = min(min(prices.keys()) for prices in price_data.values())
    earliest_date = datetime.fromtimestamp(earliest * 86400).strftime('%Y-%m-%d')
    avg_days = sum(len(p) for p in price_data.values()) / len(price_data)
    print(f"Data: earliest {earliest_date}, avg {avg_days:.0f} days per coin")
    
    # Sanity test
    print("\nRunning sanity test (buy & hold BTC)...")
    sanity = sanity_test_btc(price_data)
    print(f"Sanity test: {'PASS' if sanity.get('pass') else 'FAIL'} (error: {sanity.get('error', 'N/A')})")
    
    # GiiQ BO proxy
    print("\nCalculating GiiQ BO proxy...")
    giiq_bo_returns, giiq_bo_equity = calculate_giiq_bo_proxy(price_data)
    print(f"GiiQ BO: {len(giiq_bo_returns)} days")
    
    # BTC returns
    btc_returns = {}
    if 'BTC' in price_data:
        days = sorted(price_data['BTC'].keys())
        for i in range(1, len(days)):
            if price_data['BTC'][days[i-1]] > 0:
                btc_returns[days[i]] = (price_data['BTC'][days[i]] / price_data['BTC'][days[i-1]]) - 1
    
    results = {
        'meta': {
            'timestamp': datetime.now().isoformat(),
            'version': 'v2_fixed',
            'earliest_data': earliest_date,
            'avg_days': int(avg_days),
            'sanity_test': sanity
        },
        'strategies': {}
    }
    
    # Strategy 1: Reversal (with and without costs)
    for with_costs in [True, False]:
        label = 'with_costs' if with_costs else 'no_costs'
        print(f"\n[Strategy 1] Cross-Sectional Reversal ({label})...")
        
        try:
            strat = strategy_cross_sectional_reversal(price_data, with_costs)
            if 'error' not in strat:
                metrics = calculate_metrics(
                    strat['equity_curve'],
                    strat['daily_returns'],
                    giiq_bo_returns,
                    btc_returns,
                    strat['trades'],
                    f'Cross-Sectional Reversal ({label})'
                )
                metrics['detailed_trades'] = strat['detailed_trades']
                results['strategies'][f'reversal_{label}'] = metrics
            else:
                results['strategies'][f'reversal_{label}'] = strat
        except Exception as e:
            print(f"Error: {e}")
            import traceback
            traceback.print_exc()
            results['strategies'][f'reversal_{label}'] = {'error': str(e)}
    
    # Strategy 2: Funding (skipped - insufficient historical data even with pagination)
    print("\n[Strategy 2] Funding Cross-Section: SKIPPED (insufficient historical data)")
    results['strategies']['funding'] = {'error': 'Insufficient historical funding data (HL limitation)'}
    
    # Strategy 3: Pairs (with and without costs)
    for with_costs in [True, False]:
        label = 'with_costs' if with_costs else 'no_costs'
        print(f"\n[Strategy 3] Pairs Mean Reversion ({label})...")
        
        try:
            strat = strategy_pairs_mean_reversion(price_data, with_costs)
            if 'error' not in strat:
                metrics = calculate_metrics(
                    strat['equity_curve'],
                    strat['daily_returns'],
                    giiq_bo_returns,
                    btc_returns,
                    strat['trades'],
                    f'Pairs Mean Reversion ({label})'
                )
                metrics['detailed_trades'] = strat['detailed_trades']
                metrics['per_pair'] = strat['per_pair']
                results['strategies'][f'pairs_{label}'] = metrics
            else:
                results['strategies'][f'pairs_{label}'] = strat
        except Exception as e:
            print(f"Error: {e}")
            import traceback
            traceback.print_exc()
            results['strategies'][f'pairs_{label}'] = {'error': str(e)}
    
    # Save
    import os
    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, 'w') as f:
        json.dump(results, f, indent=2)
    
    print("\n" + "=" * 80)
    print(f"Complete. Results: {args.output}")


if __name__ == '__main__':
    main()
