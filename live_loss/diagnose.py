#!/usr/bin/env python3
"""Weekly live-loss diagnosis (Prism draft for Forge). Read-only: reads a trade log CSV, writes files. No orders, no keys.
Usage: python3 diagnose.py [--trades CSV_PATH_OR_URL] [--week 2026-W40 | --all] [--wallet 0x..] [--nav 1680.44] [--out out/]
--wallet: public HL address -> point-in-time NAV (portfolio accountValueHistory) + fill-count cross-check (public info API, no key).
Rules are versioned by deploy time (RULES below); a trade is checked only against the rule set live at its entry.
Env overrides (for CI): LIVE_LOSS_TRADES, LIVE_LOSS_WALLET, LIVE_LOSS_MCAP, LIVE_LOSS_BT, LIVE_LOSS_TRADES_TOKEN (optional bearer for a URL; never printed).
v2 2026-10-05 (Prism): rule versioning + point-in-time NAV + HL cross-check (fixes W40 false RULE_BREACH, see out/2026-W40/forge_check.md).
Default week = last full ISO week (Mon 00:00 → Sun 24:00 HKT) by close time. Rules = Logic Card GIIQ-BO-SOT-v1.1 (§3–§6, §9)."""
import argparse,io,json,math,os,re,urllib.request
import pandas as pd,numpy as np
Q='/workspace/giiq-quant/strategies'; E=os.environ.get
ap=argparse.ArgumentParser()
ap.add_argument('--trades',default=E('LIVE_LOSS_TRADES',f'{Q}/trade_logs/hl_live_trades.csv'))
ap.add_argument('--wallet',default=E('LIVE_LOSS_WALLET'))
ap.add_argument('--week'); ap.add_argument('--all',action='store_true')
ap.add_argument('--nav',type=float,default=1680.44,help='fallback NAV if no --wallet')
ap.add_argument('--mcap',default=E('LIVE_LOSS_MCAP','/workspace/otr-main-68186ec/data/mcap_cache.json'))
ap.add_argument('--bt',default=E('LIVE_LOSS_BT',f'{Q}/giiq_bo/results.json'))
ap.add_argument('--v11-effective',default='2026-10-03 23:20',help='HKT time Tiny 3x/2%% (SoT-4, main 5255a4c) went live; before it these are CARD_GAP, not breaches')
ap.add_argument('--strict',action='store_true',help='exit 2 if trade log incomplete or a check failed (CI: opens an issue)')
ap.add_argument('--out',default=os.path.join(os.path.dirname(os.path.abspath(__file__)),'out'))
a=ap.parse_args()
CARD='GIIQ-BO-SOT-v1.1'
# Rule sets by effective deploy time (HKT). Source: Railway deploy log, out/2026-W40/forge_check.md §1.2/§1.5. Bump when the card changes.
RULES=[('pre-SoT-2 (938aab3)','2000-01-01',dict(lev=(1,5),margin_max=0.08,coin_notional=None,fills_day=None)),
       ('GIIQ-SoT-2','2026-09-28 12:47',dict(lev=(3,5),margin_max=0.04,coin_notional=None,fills_day=3)),
       ('GIIQ-SoT-3','2026-09-28 17:46',dict(lev=(3,5),margin_max=0.04,coin_notional=0.20,fills_day=3))]
def rule_at(ts):
  r=RULES[0]
  for x in RULES:
    if ts>=pd.Timestamp(x[1]): r=x
  return r
def read_trades(src):
  if re.match(r'https?://',src):
    h={'Authorization':'Bearer '+E('LIVE_LOSS_TRADES_TOKEN')} if E('LIVE_LOSS_TRADES_TOKEN') else {}
    try: src=io.StringIO(urllib.request.urlopen(urllib.request.Request(src,headers=h),timeout=30).read().decode())
    except Exception as e: raise SystemExit(f'trade log fetch failed: {type(e).__name__} {getattr(e,"code","")}')   # never echo the URL/token
  return pd.read_csv(src,parse_dates=['time_open_hkt','time_close_hkt'])
def hl(body,tries=4):
  import time
  for i in range(tries):
    try:
      r=urllib.request.Request('https://api.hyperliquid.xyz/info',data=json.dumps(body).encode(),headers={'Content-Type':'application/json'})
      return json.load(urllib.request.urlopen(r,timeout=30))
    except Exception:
      if i==tries-1: raise
      time.sleep(5*2**i)   # HL 429s under bursts: 5,10,20s
t=read_trades(a.trades)
need=['time_open_hkt','time_close_hkt','symbol','notional_usd','leverage','fees','pnl_usd','exit_reason','source']
miss=[c for c in need if c not in t.columns]
if miss: raise SystemExit(f'trade log missing columns {miss}')
t['net']=t.pnl_usd.astype(float)   # pnl_usd is net of fees and funding (Forge export)
t['gross']=t.source.str.extract(r'gross_closedPnl=(-?[\d.]+)')[0].astype(float)
t['funding']=t.source.str.extract(r'funding=(-?[\d.]+)')[0].astype(float).fillna(0)
t['sot']=np.where(t.source.str.contains('SoT_auto'),'SoT',np.where(t.source.str.contains('pre_SoT'),'pre-SoT','unknown'))
t['hold_h']=(t.time_close_hkt-t.time_open_hkt).dt.total_seconds()/3600
t['margin']=t.notional_usd/t.leverage
# point-in-time NAV: last HL accountValue point at/before entry (public portfolio API); fallback constant --nav
navh=None; nav_src=f'constant {a.nav}'
if a.wallet:
  try:
    pts={int(ms):float(v) for k,v in hl({'type':'portfolio','user':a.wallet}) if k in('day','week','month','allTime') for ms,v in v['accountValueHistory'] if float(v)>0}
    navh=pd.Series(pts).sort_index(); navh.index=pd.to_datetime(navh.index,unit='ms').tz_localize('UTC').tz_convert('Asia/Hong_Kong').tz_localize(None)
    nav_src='HL portfolio accountValueHistory (point-in-time)'
  except Exception as e: nav_src=f'constant {a.nav} (HL portfolio fetch failed: {type(e).__name__} {getattr(e,"code","")})'
def nav_at(ts):
  if navh is None: return a.nav
  x=navh[navh.index<=ts]; return float(x.iloc[-1]) if len(x) else a.nav
t['nav']=t.time_open_hkt.map(nav_at)
# tier by circulating mcap (SoT mcap_tiers thresholds; unknown -> tiny)
mc=json.load(open(a.mcap)) if os.path.exists(a.mcap) else {}
def tier(s):
  v=mc.get(s); v=v if isinstance(v,(int,float)) else None
  if v is None: return 'tiny?'
  return 'mega' if v>=50e9 else 'large' if v>=2e9 else 'small' if v>=200e6 else 'tiny'
t['tier']=t.symbol.map(tier)
# --- week selection
now=pd.Timestamp.now(tz='Asia/Hong_Kong').tz_localize(None)
if a.all: w0,w1,label=t.time_close_hkt.min().normalize(),now,'all'
else:
  if a.week: y,w=a.week.split('-W'); w0=pd.Timestamp.fromisocalendar(int(y),int(w),1)
  else: w0=(now.normalize()-pd.Timedelta(days=now.weekday()))-pd.Timedelta(days=7)
  w1=w0+pd.Timedelta(days=7); label=f'{w0.isocalendar()[0]}-W{w0.isocalendar()[1]:02d}'
W=t[(t.time_close_hkt>=w0)&(t.time_close_hkt<w1)].copy()
day_fills=t.groupby(t.time_open_hkt.dt.date).size()
def in_schedule(ts):
  m=ts.hour*60+ts.minute
  base=8*60+55<=m<=9*60+0+5          # 08:55 executor (+5 min tolerance)
  pend=ts.hour in (0,4,8,12,16,20) and 10<=ts.minute<=20   # 4H :10 job -> pending Chase fills
  return base or pend
def brk(fl): return [f for f in fl if f!='MISSING_EXIT_REASON' and not f.startswith(('CARD_GAP','INFO:'))]
def classify(r):
  flags=[]; rn,_,R_=rule_at(r.time_open_hkt); nav=r.nav
  if r.sot!='SoT': flags.append('OFF_SOT')
  if not in_schedule(r.time_open_hkt):   # info only: no re-run time rule in force (Logic Card O-4 OPEN)
    flags.append('INFO:LATE_RERUN' if r.time_open_hkt.hour==9 and r.time_open_hkt.minute>5 else 'INFO:OFF_SCHEDULE_ENTRY')
  er=str(r.exit_reason) if pd.notna(r.exit_reason) else ''
  if not er: flags.append('MISSING_EXIT_REASON')
  big=r.tier in ('mega','large')
  if er and (('Mega/Large' in er) != big) and r.tier!='tiny?' : flags.append('EXIT_TIER_MISMATCH')
  if pd.notna(r.leverage):
    lo,hi=R_['lev']
    if r.leverage>hi: flags.append(f'LEV_GT_{hi}X')
    if r.leverage<lo: flags.append(f'LEV_LT_{lo}X')
    v11=a.v11_effective is not None and r.time_open_hkt>=pd.Timestamp(a.v11_effective)
    tag='' if v11 else 'CARD_GAP:'
    if r.tier.startswith('tiny') and r.leverage>3: flags.append(tag+'TINY_LEV_GT_3X(v1.1)')
    if r.margin>R_['margin_max']*nav*1.05: flags.append(f"MARGIN_GT_{R_['margin_max']*100:.0f}PCT_NAV")
    if r.tier.startswith('tiny') and r.margin>0.02*nav*1.05: flags.append(tag+'TINY_MARGIN_GT_2PCT(v1.1)')
  if R_['coin_notional'] and r.notional_usd>R_['coin_notional']*nav*1.02: flags.append('COIN_NOTIONAL_GT_20PCT_NAV')
  if R_['fills_day'] and day_fills.get(r.time_open_hkt.date(),0)>R_['fills_day']: flags.append('GT_3_FILLS_DAY')
  hard='hard sl' in er.lower() or 'stop' in er.lower()
  cost=float(r.fees)+max(-r.funding,0)
  if r.net<0:
    if 'OFF_SOT' in flags: cause='OFF_SOT (not under Logic Card)'
    elif brk(flags): cause='RULE_BREACH: '+','.join(brk(flags))
    elif hard: cause='HARD_SL_HIT'
    elif cost>=0.5*abs(r.net): cause='COST_DOMINATED'
    elif r.hold_h<24: cause='FAST_FAIL (<24h: false breakout)'
    elif er: cause='TIER_EXIT_NORMAL (rule-consistent loss)'
    else: cause='UNEXPLAINED (no exit reason)'
  else: cause=''
  return pd.Series({'flags':';'.join(flags),'cause':cause,'rule_set':rn})
if len(W): W[['flags','cause','rule_set']]=W.apply(classify,axis=1)
def kpis(x):
  if not len(x): return dict(n=0)
  w=x.net[x.net>0].sum(); l=-x.net[x.net<0].sum()
  return dict(n=int(len(x)),net=round(float(x.net.sum()),2),pf=(round(float(w/l),2) if l>0 else None),win_rate=round(float((x.net>0).mean()),3),
    fees=round(float(x.fees.sum()),2),funding=round(float(x.funding.sum()),2),avg_hold_h=round(float(x.hold_h.mean()),1))
bt=json.load(open(a.bt))['base']['OOS'] if os.path.exists(a.bt) else {}
p=bt.get('win_rate',0.33)
def streak(x):
  s=m=0
  for v in x.sort_values('time_close_hkt').net: s=s+1 if v<=0 else 0; m=max(m,s)
  return m
st=streak(t[t.sot=='SoT']) if (t.sot=='SoT').any() else 0
# P(at least one losing run >= st among n SoT trades) at backtest win rate p (exact DP)
def p_run(n,k,q):
  if k==0: return 1.0
  dp=np.zeros(k); dp[0]=1.0; hit=0.0
  for _ in range(n):
    nd=np.zeros(k); nd[0]=dp.sum()*(1-q)
    for j in range(1,k): nd[j]=dp[j-1]*q
    hit+=dp[k-1]*q; dp=nd
  return hit
nS=int((t.sot=='SoT').sum())
R=dict(week=label,window_hkt=[str(w0),str(w1)],card=CARD,nav_source=nav_src,trades_source=('URL' if re.match(r'https?://',a.trades) else os.path.basename(a.trades)),last_close_in_log=str(t.time_close_hkt.max()),generated_hkt=str(now)[:16],
  week_all=kpis(W),week_sot=kpis(W[W.sot=='SoT']) if len(W) else {},week_pre_sot=kpis(W[W.sot!='SoT']) if len(W) else {},
  to_date_sot=kpis(t[t.sot=='SoT']),backtest_ref=dict(win_rate=p,source='giiq_bo/results.json OOS (old research rule)'),
  sot_max_losing_streak=st,p_streak_given_bt=round(p_run(nS,st,1-p),3),
  losing_causes=(W[W.net<0].cause.value_counts().to_dict() if len(W) else {}),
  needs_attention=[])
if len(W):
  br=W[W.cause.str.startswith('RULE_BREACH')]; 
  if len(br): R['needs_attention'].append(f'{len(br)} losing trade(s) broke a Logic Card rule -> Forge/Harbor check')
  gap=W[W['flags'].str.contains('CARD_GAP')]
  if len(gap): R['needs_attention'].append(f'{len(gap)} trade(s) violate v1.1-only rules (Tiny 3x/2%) entered before Tiny 3x/2% went live ({a.v11_effective} HKT): CARD_GAP, not a breach')
  if (W.cause=='UNEXPLAINED (no exit reason)').any(): R['needs_attention'].append('exit reason missing in log -> Forge exporter fix')
# completeness: HL close fills in window vs trade-log rows (public info API)
if a.wallet and not a.all:
  try:
    ms=lambda x:int(pd.Timestamp(x).tz_localize('Asia/Hong_Kong').timestamp()*1000)
    f=hl({'type':'userFillsByTime','user':a.wallet,'startTime':ms(w0),'endTime':ms(w1)})
    oc=sorted({(x['coin'],x['oid']) for x in f if x['dir'].startswith('Close')})
    exit_oids=set(re.findall(r'\d{9,}',' '.join(W.source.str.extract(r'exit oid ([\d,]+)')[0].fillna('')))) if len(W) else set()
    missing=[c for c,o in oc if str(o) not in exit_oids]
    R['hl_check']=dict(hl_close_orders=len(oc),missing_in_log=missing)
    if missing: R['needs_attention'].append(f'trade log incomplete: {len(missing)} HL close order(s) not in log ({",".join(sorted(set(missing)))}) -> Forge exporter')
  except Exception as e: R['hl_check']=f'failed: {type(e).__name__}'; R['needs_attention'].append('HL completeness check failed (network?) -> rerun')
if a.wallet and 'failed' in nav_src: R['needs_attention'].append('point-in-time NAV unavailable, margin % uses constant NAV -> rerun')
if R['p_streak_given_bt']<0.05: R['needs_attention'].append(f'SoT losing streak {st} has p<0.05 vs backtest win rate -> Cove review')
od=os.path.join(a.out,label); os.makedirs(od,exist_ok=True)
cols=['time_open_hkt','time_close_hkt','symbol','tier','sot','rule_set','nav','notional_usd','leverage','margin','hold_h','net','fees','funding','exit_reason','flags','cause']
(W[W.net<0][cols] if len(W) else pd.DataFrame(columns=cols)).to_csv(f'{od}/losing_trades.csv',index=False)
json.dump(R,open(f'{od}/diagnosis.json','w'),indent=1,default=str)
L=[f'# Live-loss diagnosis {label} (HL, card {CARD}) — generated {R["generated_hkt"]} HKT',
   f'Window (close time, HKT): {w0:%Y-%m-%d} → {w1:%Y-%m-%d}. Read-only. Bot: read this + diagnosis.json, write conclusions.md.',f"NAV: {nav_src}. Trade log last close: {R['last_close_in_log']}. HL check: {R.get('hl_check','not run (no --wallet)')}.",'',
   '| Scope | Trades | Net US$ | PF | Win rate | Fees | Funding |','|---|---|---|---|---|---|---|']
for k in ('week_all','week_sot','week_pre_sot','to_date_sot'):
  v=R[k] or {}
  L.append(f"| {k} | {v.get('n',0)} | {v.get('net','-')} | {v.get('pf','-')} | {v.get('win_rate','-')} | {v.get('fees','-')} | {v.get('funding','-')} |")
L+=['',f"SoT max losing streak to date: {st} of {nS} trades; P(run ≥ {st} | backtest win rate {p:.0%}) = {R['p_streak_given_bt']}.",'','## Losing trades by cause']
L+=[f'- {k}: {v}' for k,v in R['losing_causes'].items()] or ['- none']
L+=['','## Losing trades']+([f"- {r.time_open_hkt:%m-%d %H:%M} {r.symbol} ({r.tier}, {r.sot}, {r.rule_set}, {r.leverage}x, margin {r.margin/r.nav:.1%} NAV, {r.hold_h:.0f}h) {r.net:+.2f} — {r.cause}" + (f" [flags: {r.flags}]" if r.flags else '') for r in W[W.net<0].itertuples()] if len(W) else ['- none'])
L+=['','## Needs attention']+([f'- {x}' for x in R['needs_attention']] or ['- nothing'])
open(f'{od}/diagnosis.md','w').write('\n'.join(L)+'\n'); print('\n'.join(L))
if a.strict and any(('incomplete' in x) or ('failed' in x) or ('unavailable' in x) for x in R['needs_attention']): raise SystemExit(2)
