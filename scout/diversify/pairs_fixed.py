#!/usr/bin/env python3
"""Fixed pairs strategy for testing"""

import math
import statistics
from typing import Dict, Any, Tuple, List
from collections import defaultdict
from datetime import datetime


def strategy_pairs_mean_reversion_fixed(
    price_data: Dict[str, Dict[int, float]],
    with_costs: bool = True
) -> Dict[str, Any]:
    """Strategy 3: Pairs log-spread z-score mean reversion - FIXED VERSION
    
    Spec (fixed):
    - Pairs: BTC/ETH, BTC/SOL, ETH/SOL, SOL/ARB
    - Lookback: 30 days for z-score
    - Entry: |z| > 2
    - Exit: z crosses 0
    - Stop: |z| > 4
    - Sizing: 25% capital per pair, 12.5% per leg, dollar-neutral
    - Hedge ratio: 1:1 in log space
    - Direction: SHORT spread when z>2 (sell expensive, buy cheap)
               LONG spread when z<-2 (buy cheap, sell expensive)
    """
    
    pairs = [
        ('BTC', 'ETH'),
        ('BTC', 'SOL'),
        ('ETH', 'SOL'),
        ('SOL', 'ARB')
    ]
    
    # Calculate log spreads and z-scores
    def calc_log_spread(prices1: Dict[int, float], prices2: Dict[int, float]) -> Dict[int, float]:
        common_days = sorted(set(prices1.keys()) & set(prices2.keys()))
        return {day: math.log(prices1[day]) - math.log(prices2[day]) 
                for day in common_days if prices1[day] > 0 and prices2[day] > 0}
    
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
    
    # Backtest with proper sizing
    positions = {}  # {pair: {dir, entry_day, entry_z, entry_price1, entry_price2}}
    equity_curve = {all_days[0]: 1.0}
    trades = []
    detailed_trades = []
    
    COST_PER_SIDE = (0.00045 + 0.0005) if with_costs else 0.0
    CAPITAL_PER_PAIR = 0.25  # 25% per pair
    
    for i, day in enumerate(all_days):
        if i == 0:
            continue
        
        prev_day = all_days[i-1]
        daily_pnl = 0.0
        
        # Mark-to-market existing positions
        for pair_name, pos_info in list(positions.items()):
            if pair_name not in pair_signals or day not in pair_signals[pair_name]:
                continue
            
            z = pair_signals[pair_name][day]
            coin1, coin2 = pair_name.split('/')
            
            # Daily P&L from spread movement
            if coin1 in price_data and coin2 in price_data:
                if day in price_data[coin1] and day in price_data[coin2]:
                    if prev_day in price_data[coin1] and prev_day in price_data[coin2]:
                        # Log return of spread
                        prev_spread = math.log(price_data[coin1][prev_day]) - math.log(price_data[coin2][prev_day])
                        curr_spread = math.log(price_data[coin1][day]) - math.log(price_data[coin2][day])
                        spread_move = curr_spread - prev_spread
                        
                        # P&L: dir * spread_move * capital
                        # dir = 1 means long spread (profit when spread widens)
                        # dir = -1 means short spread (profit when spread narrows)
                        pnl = pos_info['dir'] * spread_move * CAPITAL_PER_PAIR
                        daily_pnl += pnl
            
            # Check exit conditions
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
            
            # Exit
            if exit_trade or stop_trade:
                # Calculate trade P&L
                if coin1 in price_data and coin2 in price_data:
                    if day in price_data[coin1] and day in price_data[coin2]:
                        entry_spread = math.log(pos_info['entry_price1']) - math.log(pos_info['entry_price2'])
                        exit_spread = math.log(price_data[coin1][day]) - math.log(price_data[coin2][day])
                        spread_change = exit_spread - entry_spread
                        
                        trade_pnl = pos_info['dir'] * spread_change * CAPITAL_PER_PAIR
                        
                        # Exit cost: 2 legs
                        exit_cost = CAPITAL_PER_PAIR * 2 * COST_PER_SIDE
                        daily_pnl -= exit_cost
                        
                        # Store detailed trade
                        if len(detailed_trades) < 10:
                            detailed_trades.append({
                                'entry_date': datetime.fromtimestamp(pos_info['entry_day'] * 86400).strftime('%Y-%m-%d'),
                                'exit_date': datetime.fromtimestamp(day * 86400).strftime('%Y-%m-%d'),
                                'pair': pair_name,
                                'direction': 'LONG spread' if pos_info['dir'] > 0 else 'SHORT spread',
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
                        
                        trades.append({
                            'day': day,
                            'pair': pair_name,
                            'action': f'exit_{reason}',
                            'z': z,
                            'trade_pnl': trade_pnl,
                            'cost': exit_cost
                        })
                
                del positions[pair_name]
        
        # Entry signals
        for pair_name, signals in pair_signals.items():
            if day not in signals:
                continue
            
            if pair_name in positions:
                continue
            
            z = signals[day]
            coin1, coin2 = pair_name.split('/')
            
            if coin1 not in price_data or coin2 not in price_data:
                continue
            if day not in price_data[coin1] or day not in price_data[coin2]:
                continue
            
            entry_dir = 0
            
            # Entry logic:
            # When z > 2: spread is HIGH -> SHORT spread (sell coin1, buy coin2)
            # When z < -2: spread is LOW -> LONG spread (buy coin1, sell coin2)
            if z > 2:
                entry_dir = -1  # Short spread
            elif z < -2:
                entry_dir = 1  # Long spread
            
            if entry_dir != 0:
                # Entry cost: 2 legs
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
                
                trades.append({
                    'day': day,
                    'pair': pair_name,
                    'action': 'enter_long' if entry_dir > 0 else 'enter_short',
                    'z': z,
                    'cost': entry_cost
                })
        
        # Update equity
        equity_curve[day] = equity_curve[prev_day] * (1 + daily_pnl)
    
    # Build daily returns
    daily_returns = {}
    for i in range(1, len(all_days)):
        day = all_days[i]
        prev_day = all_days[i-1]
        daily_returns[day] = (equity_curve[day] / equity_curve[prev_day]) - 1
    
    returns = [daily_returns[d] for d in sorted(daily_returns.keys())]
    
    # Per-pair metrics
    per_pair_metrics = {}
    for coin1, coin2 in pairs:
        pair_str = f'{coin1}/{coin2}'
        pair_trades = [t for t in trades if t.get('pair') == pair_str]
        
        per_pair_metrics[pair_str] = {
            'trades': len(pair_trades),
            'entries': len([t for t in pair_trades if 'enter' in t.get('action', '')]),
            'exits': len([t for t in pair_trades if 'exit' in t.get('action', '')])
        }
    
    return {
        'equity_curve': equity_curve,
        'returns': returns,
        'trades': trades,
        'daily_returns': daily_returns,
        'per_pair': per_pair_metrics,
        'detailed_trades': detailed_trades,
        'with_costs': with_costs
    }
