#!/usr/bin/env python3
"""Scout candidate discovery. Usage: python3 discover.py [--top-n 10] [--timeout-per-screen 600]
Discovers candidates from public, no-key sources:
1. HL vaults (filter + rank by risk-adjusted return, then screen top N)
2. Funding spreads (HL, Binance, Bybit, OKX)
3. Open-source bots (GitHub search API)
Outputs: out/YYYY-MM-DD.json (full data) + out/latest.md (<= 1 page summary)
"""
import json
import sys
import time
import urllib.request
import urllib.error
import argparse
import os
import subprocess
from datetime import datetime, timedelta, timezone

HL_API = 'https://api.hyperliquid.xyz/info'
HL_STATS = 'https://stats-data.hyperliquid.xyz/Mainnet'
GITHUB_API = 'https://api.github.com'
BINANCE_API = 'https://fapi.binance.com'
BYBIT_API = 'https://api.bybit.com'
OKX_API = 'https://www.okx.com'

def http_get(url, headers=None, timeout=30):
    """HTTP GET with timeout and error handling"""
    try:
        req = urllib.request.Request(url, headers=headers or {})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        if e.code in (403, 451):  # geo-block
            return None
        raise
    except Exception:
        return None

def http_post(url, body, headers=None, timeout=30):
    """HTTP POST with timeout and error handling"""
    try:
        h = {'Content-Type': 'application/json'}
        if headers:
            h.update(headers)
        req = urllib.request.Request(url, json.dumps(body).encode(), h)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return None

def hl_post(body):
    """HL info API with retries"""
    for i in range(5):
        try:
            result = http_post(HL_API, body, timeout=30)
            if result is not None:
                return result
        except Exception:
            pass
        time.sleep(2 * (i + 1))
    return None

def fetch_hl_vaults():
    """Fetch HL vaults from stats-data API"""
    try:
        data = http_get(f'{HL_STATS}/vaults', timeout=30)
        return data or []
    except Exception as e:
        return []

def fetch_hl_meta():
    """Fetch HL metaAndAssetCtxs for funding rates"""
    return hl_post({'type': 'metaAndAssetCtxs'})

def fetch_binance_funding():
    """Fetch Binance perpetual funding rates (last 30 days avg)"""
    try:
        # Get premium index (includes funding rate)
        url = f'{BINANCE_API}/fapi/v1/premiumIndex'
        data = http_get(url, timeout=15)
        if data is None:
            return None, 'geo-block or error'
        
        # Map to coin -> 8h funding rate
        rates = {}
        for item in data:
            symbol = item.get('symbol', '')
            if not symbol.endswith('USDT'):
                continue
            coin = symbol[:-4]  # remove USDT
            try:
                rate = float(item.get('lastFundingRate', 0))
                rates[coin] = rate * 3 * 365 * 100  # 8h -> annualized %
            except (ValueError, TypeError):
                continue
        
        return rates, None
    except Exception as e:
        return None, str(e)

def fetch_bybit_funding():
    """Fetch Bybit perpetual funding rates"""
    try:
        url = f'{BYBIT_API}/v5/market/tickers?category=linear'
        data = http_get(url, timeout=15)
        if data is None:
            return None, 'geo-block or error'
        
        rates = {}
        result = data.get('result', {})
        for item in result.get('list', []):
            symbol = item.get('symbol', '')
            if not symbol.endswith('USDT'):
                continue
            coin = symbol[:-4]
            try:
                rate = float(item.get('fundingRate', 0))
                rates[coin] = rate * 3 * 365 * 100  # 8h -> annualized %
            except (ValueError, TypeError):
                continue
        
        return rates, None
    except Exception as e:
        return None, str(e)

def fetch_okx_funding():
    """Fetch OKX perpetual funding rates"""
    try:
        url = f'{OKX_API}/api/v5/public/funding-rate?instType=SWAP'
        data = http_get(url, timeout=15)
        if data is None:
            return None, 'geo-block or error'
        
        rates = {}
        for item in data.get('data', []):
            inst_id = item.get('instId', '')
            if not inst_id.endswith('-USDT-SWAP'):
                continue
            coin = inst_id.split('-')[0]
            try:
                rate = float(item.get('fundingRate', 0))
                rates[coin] = rate * 3 * 365 * 100  # 8h -> annualized %
            except (ValueError, TypeError):
                continue
        
        return rates, None
    except Exception as e:
        return None, str(e)

def fetch_hl_funding():
    """Fetch HL funding rates (30-day average from metaAndAssetCtxs)"""
    try:
        meta = fetch_hl_meta()
        if not meta:
            return {}, None
        
        universe = meta[0].get('universe', [])
        rates = {}
        
        for asset in universe:
            coin = asset.get('name', '')
            if not coin:
                continue
            
            # Get funding rate (in basis points per day, convert to APR %)
            funding = asset.get('funding', '0')
            try:
                rate = float(funding) * 365  # already in % per day
                rates[coin] = rate
            except (ValueError, TypeError):
                continue
        
        return rates, None
    except Exception as e:
        return {}, str(e)

def search_github_bots(token=None):
    """Search GitHub for trading bots"""
    keywords = [
        'funding arbitrage',
        'liquidation trading',
        'order flow',
        'market making crypto',
        'hyperliquid'
    ]
    
    results = []
    headers = {'Accept': 'application/vnd.github.v3+json'}
    if token:
        headers['Authorization'] = f'token {token}'
    
    for kw in keywords:
        try:
            query = f'{kw} language:python stars:>=200 pushed:>={datetime.now() - timedelta(days=90):%Y-%m-%d}'
            url = f'{GITHUB_API}/search/repositories?q={urllib.parse.quote(query)}&sort=stars&order=desc&per_page=10'
            
            data = http_get(url, headers=headers, timeout=15)
            if not data:
                continue
            
            for item in data.get('items', []):
                repo = item.get('full_name', '')
                stars = item.get('stargazers_count', 0)
                pushed = item.get('pushed_at', '')
                description = item.get('description', '')
                
                # Check if already in results
                if any(r['repo'] == repo for r in results):
                    continue
                
                # Try to fetch README to check for backtest results
                readme_url = f'{GITHUB_API}/repos/{repo}/readme'
                readme_data = http_get(readme_url, headers=headers, timeout=10)
                has_backtest = False
                
                if readme_data and 'content' in readme_data:
                    try:
                        import base64
                        content = base64.b64decode(readme_data['content']).decode('utf-8', errors='ignore').lower()
                        has_backtest = 'backtest' in content or 'profit' in content or 'sharpe' in content
                    except Exception:
                        pass
                
                results.append({
                    'repo': repo,
                    'stars': stars,
                    'last_push': pushed[:10],
                    'description': description[:100] if description else '',
                    'has_backtest_mention': has_backtest,
                    'criterion_9': 'needs manual review (reproducible on Railway, no 3rd-party keys)'
                })
            
            time.sleep(2)  # GitHub API rate limit
        except Exception:
            continue
    
    # Dedupe and sort by stars
    seen = set()
    unique = []
    for r in results:
        if r['repo'] not in seen:
            seen.add(r['repo'])
            unique.append(r)
    
    return sorted(unique, key=lambda x: x['stars'], reverse=True)

def filter_and_rank_vaults(vaults, rejected_addrs):
    """Filter and rank vaults by risk-adjusted return"""
    MIN_TVL = 100000  # $100k
    MIN_AGE_DAYS = 90
    MIN_RETURN = 0.0
    MAX_MDD = 0.33
    
    filtered = []
    cuts = {
        'tvl': 0,
        'age': 0,
        'return': 0,
        'mdd': 0,
        'rejected': 0,
        'no_data': 0
    }
    
    for v in vaults:
        summary = v.get('summary', {})
        if not summary:
            cuts['no_data'] += 1
            continue
        
        addr = summary.get('vaultAddress', '').lower()
        if not addr:
            cuts['no_data'] += 1
            continue
        
        # Skip rejected
        if addr in rejected_addrs:
            cuts['rejected'] += 1
            continue
        
        # Skip closed vaults
        if summary.get('isClosed', False):
            cuts['no_data'] += 1
            continue
        
        # TVL filter
        try:
            tvl = float(summary.get('tvl', 0))
            if tvl < MIN_TVL:
                cuts['tvl'] += 1
                continue
        except (ValueError, TypeError):
            cuts['no_data'] += 1
            continue
        
        # Age filter (createTimeMillis)
        try:
            inception = int(summary.get('createTimeMillis', 0))
            age_days = (time.time() * 1000 - inception) / 86400000
            if age_days < MIN_AGE_DAYS:
                cuts['age'] += 1
                continue
        except (ValueError, TypeError):
            cuts['no_data'] += 1
            continue
        
        # Get PnL data for return and MDD calculation
        pnls_dict = dict(v.get('pnls', []))
        all_time_pnls = pnls_dict.get('allTime', [])
        
        if not all_time_pnls or len(all_time_pnls) < 2:
            cuts['no_data'] += 1
            continue
        
        # Convert PnL strings to floats
        try:
            pnl_values = [float(x) for x in all_time_pnls]
        except (ValueError, TypeError):
            cuts['no_data'] += 1
            continue
        
        # Compute 90-day return (use last 13 weeks ~90 days from weekly data)
        # The pnl array represents cumulative returns at weekly intervals
        ret_90d = 0.0
        if len(pnl_values) >= 2:
            # Take most recent 13 points or all if fewer
            recent_window = min(13, len(pnl_values))
            start_val = pnl_values[-recent_window]
            end_val = pnl_values[-1]
            
            try:
                # PnL values are cumulative $ amounts, compute return
                if abs(start_val) > 0.01:
                    ret_90d = (end_val - start_val) / abs(start_val)
                elif end_val > 0:
                    ret_90d = 1.0  # positive PnL from zero
                else:
                    ret_90d = -1.0  # negative PnL from zero
            except (ValueError, TypeError, ZeroDivisionError):
                cuts['no_data'] += 1
                continue
        
        if ret_90d <= MIN_RETURN:
            cuts['return'] += 1
            continue
        
        # MDD filter - compute from cumulative PnL series
        mdd = 0.0
        try:
            peak = pnl_values[0]
            for val in pnl_values:
                peak = max(peak, val)
                if peak > 0:
                    dd = (peak - val) / peak
                    mdd = max(mdd, dd)
                elif peak < 0 and val < peak:
                    # In negative territory, measure relative to least-negative peak
                    dd = (val - peak) / abs(peak)
                    mdd = max(mdd, dd)
            
            if mdd > MAX_MDD:
                cuts['mdd'] += 1
                continue
        except Exception:
            cuts['no_data'] += 1
            continue
        
        # Compute risk-adjusted return
        risk_adj = ret_90d / max(mdd, 0.01)  # avoid div by zero
        
        filtered.append({
            'address': addr,
            'name': summary.get('name', 'Unknown'),
            'tvl': tvl,
            'age_days': age_days,
            'return_90d': ret_90d,
            'mdd': mdd,
            'risk_adjusted': risk_adj
        })
    
    # Sort by risk-adjusted return
    filtered.sort(key=lambda x: x['risk_adjusted'], reverse=True)
    
    return filtered, cuts

def run_screen(address, timeout=600):
    """Run screen.py on an HL address with timeout"""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    screen_path = os.path.join(script_dir, 'screen.py')
    
    try:
        result = subprocess.run(
            ['python3', screen_path, address, '--json'],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=script_dir
        )
        
        if result.returncode == 0:
            return json.loads(result.stdout), None
        else:
            return None, f'screen.py failed: {result.stderr[:200]}'
    except subprocess.TimeoutExpired:
        return None, f'screen.py timeout ({timeout}s)'
    except Exception as e:
        return None, f'screen.py error: {str(e)[:200]}'

def generate_summary(data):
    """Generate 1-page markdown summary"""
    lines = []
    lines.append(f"# Scout Daily Report — {data['date']}")
    lines.append('')
    
    # PASS candidates first
    passes = [c for c in data['hl_vaults'] if c.get('screen_result', {}).get('verdict', '').startswith('PASS')]
    if passes:
        lines.append('## PASS Gate 1')
        lines.append('')
        for c in passes[:3]:  # limit to top 3
            s = c['screen_result']
            lines.append(f"**{c['name']}** (`{c['address'][:10]}...`)")
            lines.append(f"- Copy PF: {s.get('pf_copy_full', 'n/a')}, Beta share: {s.get('beta_share_of_pnl', 0)*100:.0f}%, Net long: {s.get('net_long_time', 0)*100:.0f}%, MDD: {s.get('mdd_alltime', 0)*100:.0f}%")
            lines.append(f"- TVL: ${c['tvl']:,.0f}, 90d return: {c['return_90d']*100:.1f}%, MDD: {c['mdd']*100:.1f}%")
            lines.append('')
    else:
        lines.append('## No candidates passed gate 1 today')
        lines.append('')
    
    # Funding opportunities
    if data['funding_spreads']:
        lines.append('## Funding Opportunities')
        lines.append('')
        lines.append('| Coin | HL 30d avg | Binance | Bybit | OKX | Max spread |')
        lines.append('|------|-----------|---------|-------|-----|-----------|')
        for f in data['funding_spreads'][:10]:  # top 10
            lines.append(f"| {f['coin']} | {f['hl_rate']:.1f}% | {f['binance_rate']:.1f}% | {f['bybit_rate']:.1f}% | {f['okx_rate']:.1f}% | {f['max_spread']:.1f}% |")
        lines.append('')
    
    # GitHub bots
    if data['github_bots']:
        lines.append('## Open-source bots')
        lines.append('')
        for b in data['github_bots'][:5]:  # top 5
            lines.append(f"- **{b['repo']}** ({b['stars']} ⭐, last push {b['last_push']})")
            if b['has_backtest_mention']:
                lines.append(f"  - README mentions backtest results")
            lines.append(f"  - {b['criterion_9']}")
        lines.append('')
    
    # Stats
    lines.append('## Stats')
    lines.append('')
    lines.append(f"- Vaults screened: {len([c for c in data['hl_vaults'] if 'screen_result' in c])}")
    lines.append(f"- Vaults not screened (time limit): {len([c for c in data['hl_vaults'] if 'screen_error' in c])}")
    
    cuts = data['filter_cuts']
    lines.append(f"- Vaults cut: TVL {cuts['tvl']}, age {cuts['age']}, return {cuts['return']}, MDD {cuts['mdd']}, rejected {cuts['rejected']}, no data {cuts['no_data']}")
    
    errors = data.get('errors', [])
    if errors:
        lines.append(f"- Errors: {', '.join(errors)}")
    
    lines.append('')
    lines.append(f"Generated at {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC")
    
    return '\n'.join(lines)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--top-n', type=int, default=10, help='Number of top vaults to screen')
    parser.add_argument('--timeout-per-screen', type=int, default=600, help='Timeout per screen.py run (seconds)')
    args = parser.parse_args()
    
    print('Scout discovery starting...')
    
    # Load rejected addresses
    script_dir = os.path.dirname(os.path.abspath(__file__))
    rejected_path = os.path.join(script_dir, 'rejected.json')
    rejected_addrs = set()
    try:
        with open(rejected_path) as f:
            rejected = json.load(f)
            rejected_addrs = {r['address'].lower() for r in rejected.get('hl_vaults', [])}
    except Exception:
        pass
    
    data = {
        'date': datetime.now(timezone.utc).strftime('%Y-%m-%d'),
        'hl_vaults': [],
        'funding_spreads': [],
        'github_bots': [],
        'filter_cuts': {},
        'errors': []
    }
    
    # 1. Fetch and filter HL vaults
    print('Fetching HL vaults...')
    vaults = fetch_hl_vaults()
    if not vaults:
        data['errors'].append('Failed to fetch HL vaults')
    else:
        print(f'Fetched {len(vaults)} vaults, filtering...')
        filtered, cuts = filter_and_rank_vaults(vaults, rejected_addrs)
        data['filter_cuts'] = cuts
        print(f'Filtered to {len(filtered)} vaults (top {args.top_n} will be screened)')
        
        # Screen top N
        for i, vault in enumerate(filtered[:args.top_n]):
            print(f'Screening {i+1}/{min(len(filtered), args.top_n)}: {vault["name"]} ({vault["address"][:10]}...)')
            result, error = run_screen(vault['address'], timeout=args.timeout_per_screen)
            
            if result:
                vault['screen_result'] = result
            else:
                vault['screen_error'] = error
                print(f'  Error: {error}')
            
            data['hl_vaults'].append(vault)
            time.sleep(2)  # rate limit
        
        # Add remaining vaults as not screened
        for vault in filtered[args.top_n:]:
            vault['screen_error'] = 'not screened today (time limit)'
            data['hl_vaults'].append(vault)
    
    # 2. Fetch funding rates
    print('Fetching funding rates...')
    hl_funding, hl_err = fetch_hl_funding()
    if hl_err:
        data['errors'].append(f'HL funding: {hl_err}')
    
    binance_funding, binance_err = fetch_binance_funding()
    if binance_err:
        data['errors'].append(f'Binance unavailable ({binance_err})')
        binance_funding = {}
    
    bybit_funding, bybit_err = fetch_bybit_funding()
    if bybit_err:
        data['errors'].append(f'Bybit unavailable ({bybit_err})')
        bybit_funding = {}
    
    okx_funding, okx_err = fetch_okx_funding()
    if okx_err:
        data['errors'].append(f'OKX unavailable ({okx_err})')
        okx_funding = {}
    
    # Combine funding data
    all_coins = set(hl_funding.keys()) | set(binance_funding.keys()) | set(bybit_funding.keys()) | set(okx_funding.keys())
    
    for coin in all_coins:
        hl_rate = hl_funding.get(coin, 0)
        binance_rate = binance_funding.get(coin, 0)
        bybit_rate = bybit_funding.get(coin, 0)
        okx_rate = okx_funding.get(coin, 0)
        
        rates = [r for r in [hl_rate, binance_rate, bybit_rate, okx_rate] if r != 0]
        if len(rates) < 2:
            continue
        
        max_spread = max(rates) - min(rates)
        
        # Filter: spread >= 15% OR HL 30d avg > 10%
        if max_spread >= 15 or hl_rate > 10:
            data['funding_spreads'].append({
                'coin': coin,
                'hl_rate': hl_rate,
                'binance_rate': binance_rate,
                'bybit_rate': bybit_rate,
                'okx_rate': okx_rate,
                'max_spread': max_spread
            })
    
    # Sort by max spread
    data['funding_spreads'].sort(key=lambda x: x['max_spread'], reverse=True)
    print(f'Found {len(data["funding_spreads"])} funding opportunities')
    
    # 3. Search GitHub
    print('Searching GitHub...')
    github_token = os.environ.get('GITHUB_TOKEN')
    bots = search_github_bots(token=github_token)
    data['github_bots'] = bots
    print(f'Found {len(bots)} GitHub bots')
    
    # Generate outputs
    print('Generating outputs...')
    out_dir = os.path.join(script_dir, 'out')
    os.makedirs(out_dir, exist_ok=True)
    
    # Full JSON
    json_path = os.path.join(out_dir, f"{data['date']}.json")
    with open(json_path, 'w') as f:
        json.dump(data, f, indent=2, default=str)
    print(f'Wrote {json_path}')
    
    # Summary markdown
    summary = generate_summary(data)
    md_path = os.path.join(out_dir, 'latest.md')
    with open(md_path, 'w') as f:
        f.write(summary)
    print(f'Wrote {md_path}')
    
    print('Discovery complete!')
    
    # Exit with error if critical failures
    if 'Failed to fetch HL vaults' in data['errors']:
        sys.exit(1)

if __name__ == '__main__':
    main()
