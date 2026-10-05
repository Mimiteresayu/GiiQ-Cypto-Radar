#!/usr/bin/env python3
"""Backtest three non-trend crypto strategies with fixed parameters.
Usage: python3 backtest.py --output scout/diversify/out/backtest_results.json

Strategies:
1. Cross-sectional short-term reversal: weekly, top-50 liquid perps, long bottom quintile / short top quintile of 7-day return
2. Funding-rate cross-section: weekly, long lowest-funding / short highest-funding quintile, market-neutral
3. BTC/ETH (+ 2 others) log-spread z-score mean reversion: 30-day lookback, enter |z|>2, exit z=0, stop |z|>4

Data: Hyperliquid public info API (candleSnapshot daily/4h, fundingHistory, meta).
Note: Limited history available via public API; full survivorship-bias-free backtest requires Binance data archive.
"""

import json
import sys
import time
import urllib.request
import argparse
import math
import statistics
from collections import defaultdict
from typing import List, Dict, Any, Tuple
from datetime import datetime, timedelta


API = 'https://api.hyperliquid.xyz/info'


def post(body: Dict[str, Any], retries: int = 3) -> Any:
    """Make POST request to Hyperliquid API with retries."""
    for i in range(retries):
        try:
            req = urllib.request.Request(
                API,
                json.dumps(body).encode(),
                {'Content-Type': 'application/json'}
            )
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
        
        # Filter to top 50 by 24h volume, exclude spot and delisted
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
        # Fallback to known liquid coins
        return ['BTC', 'ETH', 'SOL', 'ARB', 'AVAX', 'MATIC', 'OP', 'DOGE', 'XRP', 'ADA']


def get_daily_candles(coin: str, start_ms: int, end_ms: int, max_candles: int = 5000) -> Dict[int, Dict[str, float]]:
    """Fetch daily candles for a coin. HL returns up to 5000 candles per request."""
    try:
        # HL candleSnapshot returns up to 5000 candles
        # For daily candles, that's ~13.7 years of history
        data = post({
            'type': 'candleSnapshot',
            'req': {
                'coin': coin,
                'interval': '1d',
                'startTime': 0,  # Request from beginning
                'endTime': end_ms
            }
        })
        
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
        
        time.sleep(0.2)  # Rate limit
        return candles
        
    except Exception as e:
        print(f"Error fetching HL candles for {coin}: {e}", file=sys.stderr)
        return {}


def get_funding_history(coin: str, start_ms: int, end_ms: int) -> Dict[int, float]:
    """Fetch historical funding rates for a coin from HL public API.
    Returns daily average funding rate (annualized %).
    HL fundingHistory is public per coin and does NOT require a user address."""
    try:
        # HL fundingHistory returns historical funding (updated every 8h)
        data = post({
            'type': 'fundingHistory',
            'coin': coin,
            'startTime': 0  # Get full history
        })
        
        # Aggregate by day
        daily_funding = defaultdict(list)
        for f in data:
            day = int(f['time']) // 86400000
            rate = float(f['fundingRate'])
            daily_funding[day].append(rate)
        
        # Average funding per day, annualized to %
        result = {}
        for day, rates in daily_funding.items():
            avg_rate = sum(rates) / len(rates)
            # Funding is per 8h period, 3x per day
            result[day] = avg_rate * 3 * 365 * 100  # Annualized as %
        
        time.sleep(0.2)  # Rate limit
        return result
        
    except Exception as e:
        print(f"Error fetching funding history for {coin}: {e}", file=sys.stderr)
        return {}


def calculate_returns(prices: Dict[int, float], window: int) -> Dict[int, float]:
    """Calculate rolling returns over a window."""
    returns = {}
    days = sorted(prices.keys())
    
    for i in range(window, len(days)):
        day = days[i]
        past_day = days[i - window]
        if prices[past_day] > 0:
            returns[day] = (prices[day] / prices[past_day]) - 1
    
    return returns


def calculate_giiq_bo_proxy(price_data: Dict[str, Dict[int, float]]) -> Tuple[Dict[int, float], Dict[int, float]]:
    """Calculate GiiQ BO proxy: long-only 20-day Donchian breakout, exit at 10-day low.
    Returns (daily_returns, equity_curve) for the same universe."""
    
    # Get common trading days
    all_days = sorted(set(day for coin_prices in price_data.values() for day in coin_prices.keys()))
    
    if len(all_days) < 30:
        return {}, {}
    
    # Calculate 20-day high and 10-day low for each coin
    positions = {}  # {coin: entry_price}
    equity_curve = {}
    daily_returns = {}
    
    # Initialize equity curve
    for i, day in enumerate(all_days):
        if i == 0:
            equity_curve[day] = 1.0
            continue
        
        if i < 20:  # Need 20 days of history for first entry
            equity_curve[day] = equity_curve[all_days[i-1]]
            daily_returns[day] = 0.0
            continue
        
        daily_pnl = 0.0
        
        # Check existing positions for exit
        for coin in list(positions.keys()):
            if coin not in price_data or day not in price_data[coin]:
                continue
            
            # Calculate 10-day low
            low_prices = [price_data[coin][all_days[j]] 
                         for j in range(max(0, i-10), i) 
                         if all_days[j] in price_data[coin]]
            
            if not low_prices:  # Skip if no historical data
                continue
            
            low_10d = min(low_prices)
            
            current_price = price_data[coin][day]
            
            # Exit if price hits 10-day low
            if current_price <= low_10d:
                entry_price = positions[coin]
                pnl = (current_price / entry_price) - 1
                daily_pnl += pnl
                del positions[coin]
        
        # Check for new entries (20-day breakout)
        for coin, prices in price_data.items():
            if coin in positions:  # Already in position
                continue
            
            if day not in prices:
                continue
            
            # Calculate 20-day high (excluding today)
            high_prices = [prices[all_days[j]] 
                          for j in range(max(0, i-20), i) 
                          if all_days[j] in prices]
            
            if not high_prices:  # Skip if no historical data
                continue
            
            high_20d = max(high_prices)
            
            current_price = prices[day]
            
            # Enter if price breaks above 20-day high
            if current_price > high_20d:
                positions[coin] = current_price
        
        # Mark-to-market existing positions
        for coin, entry_price in positions.items():
            if coin in price_data and day in price_data[coin]:
                prev_day = all_days[i-1] if i > 0 else day
                if prev_day in price_data[coin]:
                    prev_price = price_data[coin][prev_day]
                    current_price = price_data[coin][day]
                    if prev_price > 0:
                        daily_ret = (current_price / prev_price) - 1
                        daily_pnl += daily_ret
        
        # Equal weight across all positions
        if len(positions) > 0:
            daily_pnl /= len(positions)
        
        prev_equity = equity_curve[all_days[i-1]]
        equity_curve[day] = prev_equity * (1 + daily_pnl)
        daily_returns[day] = daily_pnl
    
    return daily_returns, equity_curve


def calculate_correlation(returns1: Dict[int, float], returns2: Dict[int, float]) -> float:
    """Calculate correlation between two return series."""
    # Get common days
    common_days = sorted(set(returns1.keys()) & set(returns2.keys()))
    
    if len(common_days) < 10:
        return 0.0
    
    r1 = [returns1[d] for d in common_days]
    r2 = [returns2[d] for d in common_days]
    
    # Calculate correlation
    n = len(r1)
    mean1 = sum(r1) / n
    mean2 = sum(r2) / n
    
    cov = sum((r1[i] - mean1) * (r2[i] - mean2) for i in range(n)) / n
    std1 = (sum((r1[i] - mean1) ** 2 for i in range(n)) / n) ** 0.5
    std2 = (sum((r2[i] - mean2) ** 2 for i in range(n)) / n) ** 0.5
    
    if std1 == 0 or std2 == 0:
        return 0.0
    
    return cov / (std1 * std2)


def strategy_cross_sectional_reversal(
    universe: List[str],
    start_ms: int,
    end_ms: int
) -> Dict[str, Any]:
    """Strategy 1: Cross-sectional short-term reversal.
    
    Spec (fixed):
    - Universe: top-50 liquid perps
    - Signal: 7-day return
    - Rebalance: weekly (every 7 days)
    - Positions: long bottom quintile, short top quintile (equal-weighted within each)
    - Hold: 7 days
    - Costs: 0.045% taker + 0.05% slippage per side
    """
    
    print("\n[Strategy 1] Cross-Sectional Short-Term Reversal")
    print("=" * 80)
    print("Spec: Weekly rebalance, long bottom quintile / short top quintile of 7-day return")
    print("Universe: top-50 liquid perps | Hold: 7 days | Costs: 0.045% + 0.05% per side")
    
    # Fetch data
    print("Fetching price data...")
    price_data = {}
    for coin in universe[:50]:
        candles = get_daily_candles(coin, start_ms, end_ms)
        if candles:
            price_data[coin] = {day: c['close'] for day, c in candles.items()}
    
    # Calculate 7-day returns
    returns_7d = {}
    for coin, prices in price_data.items():
        returns_7d[coin] = calculate_returns(prices, 7)
    
    # Get all trading days
    all_days = sorted(set(day for coin_rets in returns_7d.values() for day in coin_rets.keys()))
    
    if len(all_days) < 30:
        return {
            'error': 'Insufficient data for backtest',
            'days_available': len(all_days)
        }
    
    # Backtest: rebalance weekly
    positions = {}  # {coin: weight}
    equity = [1.0]
    equity_curve = {all_days[0]: 1.0}
    trades = []
    rebalance_days = all_days[::7]  # Every 7 days
    
    COST_PER_SIDE = 0.00045 + 0.0005  # 0.045% taker + 0.05% slippage
    
    for rebal_day in rebalance_days:
        # Get returns for this day
        day_returns = {}
        for coin in universe[:50]:
            if coin in returns_7d and rebal_day in returns_7d[coin]:
                day_returns[coin] = returns_7d[coin][rebal_day]
        
        if len(day_returns) < 20:  # Need enough coins for quintiles
            continue
        
        # Sort by 7-day return
        sorted_coins = sorted(day_returns.items(), key=lambda x: x[1])
        n = len(sorted_coins)
        quintile_size = n // 5
        
        # Long bottom quintile (losers), short top quintile (winners)
        long_coins = [c[0] for c in sorted_coins[:quintile_size]]
        short_coins = [c[0] for c in sorted_coins[-quintile_size:]]
        
        # New positions: equal-weighted, dollar-neutral
        new_positions = {}
        weight = 1.0 / max(len(long_coins), 1)
        
        for coin in long_coins:
            new_positions[coin] = weight
        for coin in short_coins:
            new_positions[coin] = -weight
        
        # Calculate turnover and costs
        turnover = 0.0
        for coin in set(list(positions.keys()) + list(new_positions.keys())):
            old_w = positions.get(coin, 0.0)
            new_w = new_positions.get(coin, 0.0)
            turnover += abs(new_w - old_w)
        
        cost = turnover * COST_PER_SIDE
        equity[-1] *= (1 - cost)
        
        if turnover > 0:
            trades.append({
                'day': rebal_day,
                'turnover': turnover,
                'cost': cost,
                'long': long_coins[:5],  # Sample
                'short': short_coins[:5]
            })
        
        positions = new_positions
        
        # Mark-to-market until next rebalance
        next_rebal_idx = rebalance_days.index(rebal_day) + 1
        next_rebal_day = rebalance_days[next_rebal_idx] if next_rebal_idx < len(rebalance_days) else all_days[-1]
        
        days_to_hold = [d for d in all_days if rebal_day < d <= next_rebal_day]
        
        for day in days_to_hold:
            pnl = 0.0
            for coin, weight in positions.items():
                if coin in price_data and day in price_data[coin]:
                    prev_day = day - 1
                    while prev_day not in price_data[coin] and prev_day >= rebal_day:
                        prev_day -= 1
                    
                    if prev_day in price_data[coin] and price_data[coin][prev_day] > 0:
                        ret = (price_data[coin][day] / price_data[coin][prev_day]) - 1
                        pnl += weight * ret
            
            equity.append(equity[-1] * (1 + pnl))
            equity_curve[day] = equity[-1]
    
    # Build daily returns dictionary
    daily_returns = {}
    for i in range(1, len(list(equity_curve.keys()))):
        days_sorted = sorted(equity_curve.keys())
        day = days_sorted[i]
        prev_day = days_sorted[i-1]
        daily_returns[day] = (equity_curve[day] / equity_curve[prev_day]) - 1
    
    # Calculate metrics
    returns = [equity[i] / equity[i-1] - 1 for i in range(1, len(equity))]
    
    return {
        'equity_curve': equity_curve,
        'returns': returns,
        'trades': trades,
        'daily_returns': daily_returns
    }


def strategy_funding_cross_section(
    universe: List[str],
    start_ms: int,
    end_ms: int
) -> Dict[str, Any]:
    """Strategy 2: Funding-rate cross-section.
    
    Spec (fixed):
    - Universe: top-50 liquid perps
    - Signal: 30-day average funding rate
    - Rebalance: weekly
    - Positions: long lowest-funding quintile, short highest-funding quintile (market-neutral, equal-weighted)
    - Hold: 7 days
    - Costs: 0.045% + 0.05% per side + funding PnL
    """
    
    print("\n[Strategy 2] Funding-Rate Cross-Section")
    print("=" * 80)
    print("Spec: Weekly rebalance, long lowest-funding / short highest-funding quintile")
    print("Universe: top-50 liquid perps | Hold: 7 days | Costs: 0.045% + 0.05% per side + funding")
    
    # Fetch price and funding data
    print("Fetching price and funding data...")
    price_data = {}
    funding_data = {}
    
    for coin in universe[:50]:
        candles = get_daily_candles(coin, start_ms, end_ms)
        if candles:
            price_data[coin] = {day: c['close'] for day, c in candles.items()}
        
        funding = get_funding_history(coin, start_ms, end_ms)
        if funding:
            funding_data[coin] = funding
    
    # Calculate 30-day average funding
    def rolling_avg_funding(funding: Dict[int, float], window: int = 30) -> Dict[int, float]:
        result = {}
        days = sorted(funding.keys())
        for i in range(window, len(days)):
            day = days[i]
            avg = sum(funding[days[j]] for j in range(i - window, i)) / window
            result[day] = avg
        return result
    
    funding_30d = {}
    for coin, funding in funding_data.items():
        funding_30d[coin] = rolling_avg_funding(funding)
    
    # Get trading days
    all_days = sorted(set(day for coin_fund in funding_30d.values() for day in coin_fund.keys()))
    
    if len(all_days) < 30:
        return {
            'error': 'Insufficient funding data',
            'days_available': len(all_days)
        }
    
    # Backtest
    positions = {}
    equity = [1.0]
    equity_curve = {all_days[0]: 1.0}
    trades = []
    rebalance_days = all_days[::7]
    
    COST_PER_SIDE = 0.00045 + 0.0005
    
    for rebal_day in rebalance_days:
        # Get 30d avg funding for this day
        day_funding = {}
        for coin in universe[:50]:
            if coin in funding_30d and rebal_day in funding_30d[coin]:
                day_funding[coin] = funding_30d[coin][rebal_day]
        
        if len(day_funding) < 20:
            continue
        
        # Sort by funding rate
        sorted_coins = sorted(day_funding.items(), key=lambda x: x[1])
        n = len(sorted_coins)
        quintile_size = n // 5
        
        # Long lowest funding, short highest funding
        long_coins = [c[0] for c in sorted_coins[:quintile_size]]
        short_coins = [c[0] for c in sorted_coins[-quintile_size:]]
        
        new_positions = {}
        weight = 1.0 / max(len(long_coins), 1)
        
        for coin in long_coins:
            new_positions[coin] = weight
        for coin in short_coins:
            new_positions[coin] = -weight
        
        # Costs
        turnover = sum(abs(new_positions.get(c, 0) - positions.get(c, 0)) 
                      for c in set(list(positions.keys()) + list(new_positions.keys())))
        cost = turnover * COST_PER_SIDE
        equity[-1] *= (1 - cost)
        
        if turnover > 0:
            trades.append({
                'day': rebal_day,
                'turnover': turnover,
                'cost': cost
            })
        
        positions = new_positions
        
        # Mark-to-market
        next_rebal_idx = rebalance_days.index(rebal_day) + 1
        next_rebal_day = rebalance_days[next_rebal_idx] if next_rebal_idx < len(rebalance_days) else all_days[-1]
        days_to_hold = [d for d in all_days if rebal_day < d <= next_rebal_day]
        
        for day in days_to_hold:
            pnl = 0.0
            funding_pnl = 0.0
            
            for coin, weight in positions.items():
                # Price PnL
                if coin in price_data and day in price_data[coin]:
                    prev_day = day - 1
                    while prev_day not in price_data[coin] and prev_day >= rebal_day:
                        prev_day -= 1
                    
                    if prev_day in price_data[coin] and price_data[coin][prev_day] > 0:
                        ret = (price_data[coin][day] / price_data[coin][prev_day]) - 1
                        pnl += weight * ret
                
                # Funding PnL (pay if long, receive if short)
                if coin in funding_data and day in funding_data[coin]:
                    daily_funding = funding_data[coin][day] / 365  # Convert annualized to daily
                    funding_pnl -= weight * daily_funding  # Negative because we pay when long
            
            total_pnl = pnl + funding_pnl
            equity.append(equity[-1] * (1 + total_pnl))
            equity_curve[day] = equity[-1]
    
    # Build daily returns dictionary
    daily_returns = {}
    for i in range(1, len(list(equity_curve.keys()))):
        days_sorted = sorted(equity_curve.keys())
        day = days_sorted[i]
        prev_day = days_sorted[i-1]
        daily_returns[day] = (equity_curve[day] / equity_curve[prev_day]) - 1
    
    returns = [equity[i] / equity[i-1] - 1 for i in range(1, len(equity))]
    
    return {
        'equity_curve': equity_curve,
        'returns': returns,
        'trades': trades,
        'daily_returns': daily_returns
    }


def strategy_pairs_mean_reversion(start_ms: int, end_ms: int) -> Dict[str, Any]:
    """Strategy 3: Pairs log-spread z-score mean reversion.
    
    Spec (fixed):
    - Pairs: BTC/ETH, BTC/SOL, ETH/SOL, SOL/ARB
    - Lookback: 30 days for z-score
    - Entry: |z| > 2
    - Exit: z crosses 0
    - Stop: |z| > 4
    - Costs: 0.045% + 0.05% per side (each leg)
    """
    
    print("\n[Strategy 3] Pairs Mean Reversion (Log-Spread Z-Score)")
    print("=" * 80)
    print("Spec: 30-day lookback, enter |z|>2, exit z=0, stop |z|>4")
    print("Pairs: BTC/ETH, BTC/SOL, ETH/SOL, SOL/ARB | Costs: 0.045% + 0.05% per side per leg")
    
    pairs = [
        ('BTC', 'ETH'),
        ('BTC', 'SOL'),
        ('ETH', 'SOL'),
        ('SOL', 'ARB')
    ]
    
    # Fetch data
    print("Fetching price data for pairs...")
    price_data = {}
    for pair in pairs:
        for coin in pair:
            if coin not in price_data:
                candles = get_daily_candles(coin, start_ms, end_ms)
                if candles:
                    price_data[coin] = {day: c['close'] for day, c in candles.items()}
    
    # Calculate log spreads and z-scores
    def calc_log_spread(prices1: Dict[int, float], prices2: Dict[int, float]) -> Dict[int, float]:
        common_days = sorted(set(prices1.keys()) & set(prices2.keys()))
        return {day: math.log(prices1[day]) - math.log(prices2[day]) for day in common_days if prices1[day] > 0 and prices2[day] > 0}
    
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
    
    # Get trading days
    all_days = sorted(set(day for signals in pair_signals.values() for day in signals.keys()))
    
    if len(all_days) < 60:
        return {
            'error': 'Insufficient data for pairs strategy',
            'days_available': len(all_days)
        }
    
    # Backtest
    positions = {}  # {pair: position_size}
    equity = [1.0]
    equity_curve = {all_days[0]: 1.0}
    trades = []
    
    COST_PER_SIDE = 0.00045 + 0.0005
    MAX_POSITION_PER_PAIR = 0.5  # 50% of equity per pair
    
    for day in all_days:
        daily_pnl = 0.0
        
        # Check existing positions
        for pair_name, pos in list(positions.items()):
            if pair_name not in pair_signals or day not in pair_signals[pair_name]:
                continue
            
            z = pair_signals[pair_name][day]
            coin1, coin2 = pair_name.split('/')
            
            # Exit conditions
            exit_trade = False
            stop_trade = False
            
            if pos > 0:  # Long spread (long coin1, short coin2)
                if z <= 0:  # Crossed zero
                    exit_trade = True
                elif z > 4:  # Stop
                    stop_trade = True
            elif pos < 0:  # Short spread (short coin1, long coin2)
                if z >= 0:
                    exit_trade = True
                elif z < -4:
                    stop_trade = True
            
            # Mark-to-market
            if coin1 in price_data and coin2 in price_data and day in price_data[coin1] and day in price_data[coin2]:
                prev_day = day - 1
                while prev_day not in price_data[coin1] or prev_day not in price_data[coin2]:
                    prev_day -= 1
                    if prev_day < all_days[0]:
                        break
                
                if prev_day in price_data[coin1] and prev_day in price_data[coin2]:
                    ret1 = (price_data[coin1][day] / price_data[coin1][prev_day]) - 1
                    ret2 = (price_data[coin2][day] / price_data[coin2][prev_day]) - 1
                    spread_ret = ret1 - ret2
                    daily_pnl += pos * spread_ret
            
            # Exit
            if exit_trade or stop_trade:
                cost = abs(pos) * 2 * COST_PER_SIDE  # 2 legs
                daily_pnl -= cost
                
                trades.append({
                    'day': day,
                    'pair': pair_name,
                    'action': 'exit' if exit_trade else 'stop',
                    'z': z,
                    'position': pos,
                    'cost': cost
                })
                
                del positions[pair_name]
        
        # Entry signals
        for pair_name, signals in pair_signals.items():
            if day not in signals:
                continue
            
            if pair_name in positions:  # Already in position
                continue
            
            z = signals[day]
            
            # Entry conditions
            if z > 2:  # Short spread (mean reversion bet)
                positions[pair_name] = -MAX_POSITION_PER_PAIR
                cost = MAX_POSITION_PER_PAIR * 2 * COST_PER_SIDE
                daily_pnl -= cost
                
                trades.append({
                    'day': day,
                    'pair': pair_name,
                    'action': 'enter_short',
                    'z': z,
                    'cost': cost
                })
            
            elif z < -2:  # Long spread
                positions[pair_name] = MAX_POSITION_PER_PAIR
                cost = MAX_POSITION_PER_PAIR * 2 * COST_PER_SIDE
                daily_pnl -= cost
                
                trades.append({
                    'day': day,
                    'pair': pair_name,
                    'action': 'enter_long',
                    'z': z,
                    'cost': cost
                })
        
        equity.append(equity[-1] * (1 + daily_pnl))
        equity_curve[day] = equity[-1]
    
    # Build daily returns dictionary
    daily_returns = {}
    for i in range(1, len(list(equity_curve.keys()))):
        days_sorted = sorted(equity_curve.keys())
        day = days_sorted[i]
        prev_day = days_sorted[i-1]
        daily_returns[day] = (equity_curve[day] / equity_curve[prev_day]) - 1
    
    returns = [equity[i] / equity[i-1] - 1 for i in range(1, len(equity))]
    
    # Calculate per-pair metrics
    per_pair_metrics = {}
    for pair_name in pairs:
        pair_str = f'{pair_name[0]}/{pair_name[1]}'
        pair_trades = [t for t in trades if t.get('pair') == pair_str]
        
        per_pair_metrics[pair_str] = {
            'trades': len(pair_trades),
            'entries': len([t for t in pair_trades if 'enter' in t.get('action', '')]),
            'exits': len([t for t in pair_trades if 'exit' in t.get('action', '') or 'stop' in t.get('action', '')])
        }
    
    return {
        'equity_curve': equity_curve,
        'returns': returns,
        'trades': trades,
        'daily_returns': daily_returns,
        'per_pair': per_pair_metrics
    }


def calculate_metrics(
    equity_curve: Dict[int, float],
    returns: List[float],
    strategy_name: str,
    trades: List[Dict[str, Any]],
    daily_returns: Dict[int, float],
    giiq_bo_returns: Dict[int, float],
    btc_returns: Dict[int, float],
    start_date_ms: int
) -> Dict[str, Any]:
    """Calculate performance metrics for a strategy."""
    
    if len(returns) == 0:
        return {'error': 'No returns to calculate metrics'}
    
    # Basic metrics
    final_equity = equity_curve[max(equity_curve.keys())]
    n_years = len(returns) / 252
    
    total_return = final_equity - 1
    cagr = (final_equity ** (1 / n_years) - 1) if n_years > 0 else 0
    
    # Sharpe (annualized, assume 0% risk-free rate)
    mean_ret = sum(returns) / len(returns)
    std_ret = statistics.stdev(returns) if len(returns) > 1 else 0
    sharpe = (mean_ret / std_ret * (252 ** 0.5)) if std_ret > 0 else 0
    
    # Max drawdown
    peak = 1.0
    max_dd = 0.0
    for day in sorted(equity_curve.keys()):
        val = equity_curve[day]
        peak = max(peak, val)
        dd = 1 - val / peak
        max_dd = max(max_dd, dd)
    
    # Profit factor (if we have trade-level data)
    wins = [r for r in returns if r > 0]
    losses = [r for r in returns if r < 0]
    
    win_sum = sum(wins)
    loss_sum = abs(sum(losses))
    profit_factor = (win_sum / loss_sum) if loss_sum > 0 else (float('inf') if win_sum > 0 else 0)
    
    # Year-by-year split (use actual calendar years)
    yearly = defaultdict(list)
    days = sorted(equity_curve.keys())
    
    for i in range(1, len(days)):
        day = days[i]
        ret = equity_curve[day] / equity_curve[days[i-1]] - 1
        # Convert day index to actual year
        day_ms = day * 86400000
        year = 1970 + day_ms // (365.25 * 86400000)
        yearly[int(year)].append(ret)
    
    yearly_returns = {}
    yearly_sharpe = {}
    yearly_mdd = {}
    yearly_trades = {}
    
    for year, rets in yearly.items():
        year_equity = 1.0
        peak = 1.0
        mdd = 0.0
        
        for r in rets:
            year_equity *= (1 + r)
            peak = max(peak, year_equity)
            dd = 1 - year_equity / peak
            mdd = max(mdd, dd)
        
        yearly_returns[year] = year_equity - 1
        
        # Yearly Sharpe
        if len(rets) > 1:
            mean_ret = sum(rets) / len(rets)
            std_ret = statistics.stdev(rets)
            yearly_sharpe[year] = (mean_ret / std_ret * (252 ** 0.5)) if std_ret > 0 else 0
        else:
            yearly_sharpe[year] = 0
        
        yearly_mdd[year] = mdd
        
        # Count trades per year
        year_start_day = int((year - 1970) * 365.25)
        year_end_day = int((year - 1970 + 1) * 365.25)
        yearly_trades[year] = sum(1 for t in trades if 'day' in t and year_start_day <= t['day'] < year_end_day)
    
    # IS/OOS split (≤2023 vs 2024-2026)
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
    corr_giiq = calculate_correlation(daily_returns, giiq_bo_returns) if giiq_bo_returns else None
    corr_btc = calculate_correlation(daily_returns, btc_returns) if btc_returns else None
    
    # Check if underpowered
    underpowered = len(trades) < 50
    
    result = {
        'strategy': strategy_name,
        'total_return': f'{total_return:.2%}',
        'cagr': f'{cagr:.2%}',
        'sharpe': f'{sharpe:.2f}',
        'max_drawdown': f'{max_dd:.2%}',
        'profit_factor': f'{profit_factor:.2f}' if profit_factor != float('inf') else 'inf',
        'num_trades': len(trades),
        'num_days': len(returns),
        'underpowered': underpowered,
        'yearly_summary': {
            str(y): {
                'return': f'{yearly_returns[y]:.2%}',
                'sharpe': f'{yearly_sharpe[y]:.2f}',
                'mdd': f'{yearly_mdd[y]:.2%}',
                'trades': yearly_trades[y]
            }
            for y in sorted(yearly_returns.keys())
        },
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
        },
        'trades_sample': trades[:5] if len(trades) > 5 else trades
    }
    
    return result


def main():
    parser = argparse.ArgumentParser(description='Backtest non-trend crypto strategies')
    parser.add_argument('--output', default='scout/diversify/out/backtest_results.json', help='Output JSON file')
    parser.add_argument('--days', type=int, default=730, help='Lookback days (default 730 = ~2 years)')
    
    args = parser.parse_args()
    
    print("=" * 80)
    print("GiiQ Diversification Strategy Backtest")
    print("=" * 80)
    print(f"Lookback: {args.days} days (requesting maximum available from HL)")
    print(f"Data source: Hyperliquid public API (candleSnapshot up to 5000 daily candles)")
    print(f"Output: {args.output}")
    print()
    
    # Date range
    end_time = int(time.time() * 1000)
    start_time = 0  # Request all available history
    
    # Get universe
    print("Fetching universe...")
    universe = get_universe()
    print(f"Universe: {len(universe)} coins - {', '.join(universe[:10])}...")
    
    # Fetch price data for universe (shared across strategies and GiiQ BO)
    print("\nFetching price data for full universe...")
    price_data = {}
    data_start_dates = {}
    
    for coin in universe:
        candles = get_daily_candles(coin, start_time, end_time)
        if candles:
            price_data[coin] = {day: c['close'] for day, c in candles.items()}
            if candles:
                data_start_dates[coin] = min(candles.keys())
    
    # Report data availability
    if data_start_dates:
        earliest_day = min(data_start_dates.values())
        earliest_date = datetime.fromtimestamp(earliest_day * 86400).strftime('%Y-%m-%d')
        avg_days = sum(len(p) for p in price_data.values()) / len(price_data)
        print(f"Data available: earliest {earliest_date}, avg {avg_days:.0f} days per coin")
    
    # Calculate GiiQ BO proxy
    print("\nCalculating GiiQ BO proxy (20-day Donchian breakout, 10-day low exit)...")
    giiq_bo_returns, giiq_bo_equity = calculate_giiq_bo_proxy(price_data)
    print(f"GiiQ BO proxy: {len(giiq_bo_returns)} days of returns")
    
    # Calculate BTC returns
    btc_returns = {}
    if 'BTC' in price_data:
        days_sorted = sorted(price_data['BTC'].keys())
        for i in range(1, len(days_sorted)):
            day = days_sorted[i]
            prev_day = days_sorted[i-1]
            if price_data['BTC'][prev_day] > 0:
                btc_returns[day] = (price_data['BTC'][day] / price_data['BTC'][prev_day]) - 1
        print(f"BTC returns: {len(btc_returns)} days")
    
    # Run strategies
    results = {
        'meta': {
            'timestamp': datetime.now().astimezone().isoformat(),
            'requested_lookback_days': args.days,
            'actual_days_available': int(avg_days) if data_start_dates else 0,
            'earliest_data': earliest_date if data_start_dates else 'N/A',
            'data_source': 'Hyperliquid public API (candleSnapshot, fundingHistory)',
            'data_limitations': f'HL provides up to 5000 daily candles per coin (~13.7 years). Earliest data: {earliest_date if data_start_dates else "N/A"}. No Binance data archive attempted (requires additional setup).',
            'universe_size': len(universe),
            'costs': {
                'taker_fee': '0.045%',
                'slippage': '0.05% per side',
                'funding': 'Historical funding rates from HL fundingHistory (per coin, no user address required)'
            },
            'giiq_bo_proxy': {
                'spec': 'Long-only 20-day Donchian breakout, exit at 10-day low',
                'days': len(giiq_bo_returns)
            }
        },
        'strategies': {}
    }
    
    try:
        print("\nRunning Strategy 1: Cross-Sectional Reversal...")
        strat1 = strategy_cross_sectional_reversal(universe, start_time, end_time)
        # Fill in correlations
        if 'error' not in strat1:
            strat1_metrics = calculate_metrics(
                strat1.get('equity_curve', {}),
                strat1.get('returns', []),
                'Cross-Sectional Reversal',
                strat1.get('trades', []),
                strat1.get('daily_returns', {}),
                giiq_bo_returns,
                btc_returns,
                start_time
            )
            results['strategies']['cross_sectional_reversal'] = strat1_metrics
        else:
            results['strategies']['cross_sectional_reversal'] = strat1
    except Exception as e:
        results['strategies']['cross_sectional_reversal'] = {'error': str(e)}
        print(f"Strategy 1 failed: {e}")
        import traceback
        traceback.print_exc()
    
    try:
        print("\nRunning Strategy 2: Funding Cross-Section...")
        strat2 = strategy_funding_cross_section(universe, start_time, end_time)
        if 'error' not in strat2:
            strat2_metrics = calculate_metrics(
                strat2.get('equity_curve', {}),
                strat2.get('returns', []),
                'Funding Cross-Section',
                strat2.get('trades', []),
                strat2.get('daily_returns', {}),
                giiq_bo_returns,
                btc_returns,
                start_time
            )
            results['strategies']['funding_cross_section'] = strat2_metrics
        else:
            results['strategies']['funding_cross_section'] = strat2
    except Exception as e:
        results['strategies']['funding_cross_section'] = {'error': str(e)}
        print(f"Strategy 2 failed: {e}")
        import traceback
        traceback.print_exc()
    
    try:
        print("\nRunning Strategy 3: Pairs Mean Reversion...")
        strat3 = strategy_pairs_mean_reversion(start_time, end_time)
        if 'error' not in strat3:
            strat3_metrics = calculate_metrics(
                strat3.get('equity_curve', {}),
                strat3.get('returns', []),
                'Pairs Mean Reversion',
                strat3.get('trades', []),
                strat3.get('daily_returns', {}),
                giiq_bo_returns,
                btc_returns,
                start_time
            )
            strat3_metrics['per_pair'] = strat3.get('per_pair', {})
            results['strategies']['pairs_mean_reversion'] = strat3_metrics
        else:
            results['strategies']['pairs_mean_reversion'] = strat3
    except Exception as e:
        results['strategies']['pairs_mean_reversion'] = {'error': str(e)}
        print(f"Strategy 3 failed: {e}")
        import traceback
        traceback.print_exc()
    
    # Save results
    with open(args.output, 'w') as f:
        json.dump(results, f, indent=2)
    
    print("\n" + "=" * 80)
    print(f"Backtest complete. Results saved to {args.output}")


if __name__ == '__main__':
    main()
