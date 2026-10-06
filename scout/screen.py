#!/usr/bin/env python3
"""Scout first-gate screen for HL vaults/wallets. Usage: python3 screen.py <0xaddr> [--days 90] [--slip 0.0005] [--json]
Outputs: beta vs BTC and vs top-20 EW basket, alpha share of PnL, net-long time share (--days window),
round-trip PF over the FULL fill history (Prism unified method, prism/jump69_reconcile/report.md):
  copy route (GATE)   : net_rt = closedPnl - max(actual fee, 0.045% notional) - slip/side + funding; PF = sum(wins)/|sum(losses)|
  vault route (ref)   : net_rt = closedPnl - actual fee + funding; PF_V = (W - 0.1*max(W-L,0)) / L  (10% PS on net profit, HWM)
  last --days PF (round trips closed in window) = recency reference only.
MDD (from HL portfolio pnl/accountValue), verdict for gate 1.
--json includes gate1_pass (true only when no hard FAIL; NOTE lines do not fail the gate).
Data: public HL info API only. No keys. Offline replay: --raw DIR (fills.json + funding.json)."""
import json,sys,time,math,urllib.request,argparse,collections,os
API='https://api.hyperliquid.xyz/info'
def post(b):
    for i in range(8):
        try:
            r=urllib.request.Request(API,json.dumps(b).encode(),{'Content-Type':'application/json'})
            return json.load(urllib.request.urlopen(r,timeout=30))
        except Exception as e:
            time.sleep(5*(i+1)); err=e   # HL info API rate-limits (429): back off up to ~3 min
    raise err
DAY=86400000
def daily_close(coin,start,end):
    c=post({'type':'candleSnapshot','req':{'coin':coin,'interval':'1d','startTime':start,'endTime':end}})
    return {int(x['t'])//DAY:float(x['c']) for x in c}
def rets(d):
    ks=sorted(d);return {k:d[k]/d[p]-1 for p,k in zip(ks,ks[1:]) if d[p]}
def ols(y,x):
    n=len(y)
    if n<10: return None,None,0
    mx=sum(x)/n;my=sum(y)/n
    vx=sum((a-mx)**2 for a in x)
    if vx==0: return None,None,n
    b=sum((a-mx)*(c-my) for a,c in zip(x,y))/vx;a=my-b*mx
    ss=sum((c-my)**2 for c in y);res=sum((c-a-b*xx)**2 for xx,c in zip(x,y))
    return b,a,(1-res/ss if ss else 0)
def portfolio(addr):
    v=None
    try:
        v=post({'type':'vaultDetails','vaultAddress':addr})
    except Exception: pass
    if v and v.get('portfolio'): return dict(v['portfolio']),v.get('name'),True
    p=post({'type':'portfolio','user':addr});return dict(p),None,False
def equity_returns(series):
    av=[(int(t),float(x)) for t,x in series['accountValueHistory']];pn=[(int(t),float(x)) for t,x in series['pnlHistory']]
    out={}
    for i in range(1,len(pn)):
        base=av[i-1][1]
        if base>0: out[pn[i][0]//DAY]=out.get(pn[i][0]//DAY,0)+(pn[i][1]-pn[i-1][1])/base
    return out
def mdd_of(r):
    idx=peak=1;m=0
    for k in sorted(r):
        idx*=1+r[k];peak=max(peak,idx);m=max(m,1-idx/peak)
    return m,idx-1
def page(typ,addr,start,end,full,key,extra={}):
    """Page forward by time with NO cap (dedupe; next page starts AT the last ms so same-ms records split by the page cut are kept).
    HL itself only serves the 10,000 most recent fills, so starting at 0 returns the newest <=10k (= full history if fewer)."""
    out=[];seen=set();s=start
    while s<=end:
        res=post({'type':typ,'user':addr,'startTime':s,'endTime':end,**extra})
        if not res: break
        new=0
        for x in res:
            k=key(x)
            if k in seen: continue
            seen.add(k);out.append(x);new+=1
        last=max(int(x['time']) for x in res)
        if len(res)<full or new==0: break
        s=last if last>s else last+1
        time.sleep(1)   # stay under HL info rate limit on long histories
    return out
def fills(addr,end):
    return page('userFillsByTime',addr,0,end,2000,lambda x:(x['tid'],x['oid'],x['time'],x['sz'],x['px'],x['closedPnl']),{'aggregateByTime':True})
def funding(addr,end):
    return page('userFunding',addr,0,end,500,lambda x:(x['time'],x['delta']['coin'],x['delta']['usdc']))
TAKER=0.00045;PS=0.10
def chain(g):
    """order same-(time,coin) fills so each startPosition follows the previous fill's end position (as Prism/Cove)"""
    if len(g)<2: return g
    end=lambda f:f['_sp']+f['_ss']
    ends={round(end(f),8) for f in g};rows=list(g)
    cur=next((f for f in rows if round(f['_sp'],8) not in ends),rows[0]);out=[]
    while rows:
        out.append(cur);rows.remove(cur)
        e=end(cur);nxt=[f for f in rows if abs(f['_sp']-e)<1e-9*max(1,abs(e))]
        cur=nxt[0] if nxt else (rows[0] if rows else None)
    return out
def round_trips(F,FU,slip):
    """Round trip = flat->flat per coin; a flip fill is split into close (|startPos|/sz share of fee/slip) + open (rest).
    Per trip: pnl=closedPnl, fee=actual fee, cfee=max(actual, 0.045% notional), slip=slip*notional (per side, each fill),
    funding=userFunding for that coin with open < t <= close. Open trips at the end are excluded."""
    for f in F:
        f['_sp']=float(f['startPosition']);f['_sz']=float(f['sz']);f['_ss']=f['_sz'] if f['side']=='B' else -f['_sz']
        f['_n']=f['_sz']*float(f['px']);f['_fee']=float(f['fee']);f['_t']=int(f['time'])
    grp=collections.defaultdict(list)
    for f in sorted(F,key=lambda x:(int(x['time']),x['tid'])):
        if f['coin'].startswith('@') or '/' in f['coin']: continue   # spot: no flat->flat perp position
        grp[(f['_t'],f['coin'])].append(f)
    tr=[];cur={}
    for k in sorted(grp,key=lambda k:k[0]):
        for f in chain(grp[k]):
            c=f['coin'];sp=f['_sp'];pa=sp+f['_ss']
            if abs(pa)<1e-12: pa=0.0
            if c not in cur: cur[c]=dict(coin=c,open=f['_t'],pnl=0.,fee=0.,cfee=0.,slip=0.,nfills=0)
            x=cur[c];flip=sp!=0 and pa!=0 and (sp>0)!=(pa>0);cf=abs(sp)/f['_sz'] if flip else 1.
            fee=f['_fee'];cfee=max(fee,TAKER*f['_n']);sl=slip*f['_n']
            x['pnl']+=float(f['closedPnl']);x['fee']+=fee*cf;x['cfee']+=cfee*cf;x['slip']+=sl*cf;x['nfills']+=1
            if pa==0 or flip:
                x['close']=f['_t'];tr.append(x);del cur[c]
                if flip: cur[c]=dict(coin=c,open=f['_t'],pnl=0.,fee=fee*(1-cf),cfee=cfee*(1-cf),slip=sl*(1-cf),nfills=1)
    fu=collections.defaultdict(list)
    for u in FU:
        d=u['delta'];fu[d['coin']].append((int(u['time']),float(d['usdc'])))
    for x in tr:
        x['funding']=sum(v for t,v in fu.get(x['coin'],()) if x['open']<t<=x['close'])
        x['net_v']=x['pnl']-x['fee']+x['funding']                    # vault deposit: actual fees + funding
        x['net_c']=x['pnl']-x['cfee']-x['slip']+x['funding']         # copy: max(fee,0.045%) + slip per side
    return tr,cur
def pf(xs):
    W=sum(v for v in xs if v>0);L=-sum(v for v in xs if v<0)
    return (W/L if L>0 else (math.inf if W>0 else None)),W,L
def pf_vault(xs):
    p,W,L=pf(xs);ps=PS*max(W-L,0)
    return ((W-ps)/L if L>0 else (math.inf if W>0 else None)),ps
def pf_block(tr,t0=None):
    t=[x for x in tr if t0 is None or x['close']>=t0]
    c,W,L=pf([x['net_c'] for x in t]);v,ps=pf_vault([x['net_v'] for x in t])
    return dict(trips=len(t),pf_copy=c,pf_vault=v,net_copy=sum(x['net_c'] for x in t),net_vault_after_ps=sum(x['net_v'] for x in t)-ps,
        wins_copy=W,losses_copy=L,closedPnl=sum(x['pnl'] for x in t),fees=sum(x['fee'] for x in t),copy_fees=sum(x['cfee'] for x in t),
        slip=sum(x['slip'] for x in t),funding=sum(x['funding'] for x in t),vault_ps=ps,
        first_close=min((x['close'] for x in t),default=None),last_close=max((x['close'] for x in t),default=None))
def main():
    ap=argparse.ArgumentParser();ap.add_argument('addr');ap.add_argument('--days',type=int,default=90);ap.add_argument('--slip',type=float,default=0.0005);ap.add_argument('--json',action='store_true');ap.add_argument('--raw',help='offline replay dir with fills.json + funding.json (HL info API dumps) instead of live fills/funding')
    a=ap.parse_args();now=int(time.time()*1000);start=now-a.days*DAY
    port,name,is_vault=portfolio(a.addr)
    # equity: use finest series covering window
    eq={}
    for key in ('allTime','month','week'):
        if key in port: eq.update({k:v for k,v in equity_returns(port[key]).items() if k*DAY>=start})
    at=equity_returns(port['allTime']) if 'allTime' in port else {}
    mdd_all,tot_all=mdd_of(at)
    mdd_win,tot_win=mdd_of(eq)
    # market
    btc=rets(daily_close('BTC',start-2*DAY,now))
    ctx=post({'type':'metaAndAssetCtxs'});uni=ctx[0]['universe'];ac=ctx[1]
    top=[u['name'] for u,c in sorted(zip(uni,ac),key=lambda z:-float(z[1].get('dayNtlVlm') or 0)) if not u.get('isDelisted')][:20]
    bk=collections.defaultdict(list)
    for c in top:
        try:
            for k,v in rets(daily_close(c,start-2*DAY,now)).items(): bk[k].append(v)
        except Exception: pass
    basket={k:sum(v)/len(v) for k,v in bk.items() if v}
    # equity series are sparse (weekly) -> aggregate market returns over same intervals
    ks=sorted(eq);pairs_b=[];pairs_k=[];prev=None
    for k in ks:
        if prev is not None:
            rb=1;rk=1
            for d in range(prev+1,k+1): rb*=1+btc.get(d,0);rk*=1+basket.get(d,0)
            pairs_b.append((eq[k],rb-1));pairs_k.append((eq[k],rk-1))
        prev=k
    bb,ab,r2b=ols([p[0] for p in pairs_b],[p[1] for p in pairs_b])
    bkb,akb,r2k=ols([p[0] for p in pairs_k],[p[1] for p in pairs_k])
    tot_mkt=sum(p[1] for p in pairs_k);tot_eq=sum(p[0] for p in pairs_k)
    beta_share=(bkb*tot_mkt/tot_eq) if (bkb is not None and tot_eq) else None
    # fills + funding: FULL history (no cap), paged forward from t=0
    if a.raw: F=json.load(open(os.path.join(a.raw,'fills.json')));FU=json.load(open(os.path.join(a.raw,'funding.json')))
    else: F=fills(a.addr,now);FU=funding(a.addr,now)
    pos=collections.defaultdict(float);px={};events=[]
    for f in sorted(F,key=lambda x:(int(x['time']),x['tid'])):
        c=f['coin'];sz=float(f['sz']);p=float(f['px']);side=1 if f['side']=='B' else -1
        pos[c]=float(f.get('startPosition',pos[c]))+side*sz;px[c]=p
        net=sum(pos[k]*px[k] for k in pos if not k.startswith('@'))
        if int(f['time'])>=start: events.append((int(f['time']),net))
    long_t=tot_t=0
    for (t0,n0),(t1,_) in zip(events,events[1:]):
        tot_t+=t1-t0;long_t+=(t1-t0) if n0>0 else 0
    net_long=long_t/tot_t if tot_t else None
    tr,open_tr=round_trips(F,FU,a.slip)
    D=lambda ms:None if ms is None else time.strftime('%Y-%m-%d',time.gmtime(ms/1000))
    full=pf_block(tr);rec=pf_block(tr,start)
    hist0=min((int(f['time']) for f in F),default=None)
    nperp=sum(1 for f in F if not (f['coin'].startswith('@') or '/' in f['coin']))
    open_fill_count=sum(x['nfills'] for x in open_tr.values())
    covered_fill_count=(nperp-open_fill_count) if nperp else 0
    cover=(covered_fill_count/nperp) if nperp else None   # share of fills inside completed round trips
    coverage_fail=cover is not None and cover<0.5
    pfc_raw=full['pf_copy']
    pfc=None if coverage_fail else pfc_raw
    rec_pfc=None if coverage_fail else rec['pf_copy']
    reasons=[]
    if beta_share is not None and beta_share>0.5 and (r2k or 0)>=0.3: reasons.append(f'beta share {beta_share:.0%} >50%')
    if net_long is not None and net_long>0.8: reasons.append(f'net long {net_long:.0%} of time >80%')
    if coverage_fail: reasons.append(f'copy-route PF n/a: completed round trips cover only {cover:.1%} of fills ({covered_fill_count}/{nperp}) <50%')
    elif pfc is None: reasons.append(f'copy PF n/a: {full["trips"]} closed round trips (position never goes flat)')
    elif pfc<=1.2: reasons.append(f'copy-route full-history PF {pfc:.2f} <=1.2')
    if mdd_all>0.33: reasons.append(f'allTime MDD {mdd_all:.0%} >33% (weekly pts, understated)')
    fu0=min((int(u['time']) for u in FU),default=None)
    if hist0 and fu0 and fu0<hist0-2*DAY: reasons.append(f'NOTE fill history truncated by HL API: funding since {D(fu0)} but fills only since {D(hist0)} ({len(F)} fills); PF covers the served fills only')
    elif len(F)>=10000: reasons.append(f'NOTE {len(F)} fills (HL may cap history near 10k); PF covers fills since {D(hist0)}')
    if open_tr: reasons.append(f'NOTE {len(open_tr)} open position(s) not in PF (no flat yet): '+','.join(sorted(open_tr)))
    gate1_pass=not [r for r in reasons if not r.startswith('NOTE')]
    verdict='PASS gate1 -> hand to Cove (IS/OOS + permutation)' if gate1_pass else 'FAIL gate1'
    res=dict(address=a.addr,name=name,is_vault=is_vault,window_days=a.days,equity_points=len(eq),
        beta_btc=bb,r2_btc=r2b,beta_top20=bkb,r2_top20=r2k,beta_share_of_pnl=beta_share,net_long_time=net_long,
        fills=len(F),funding_records=len(FU),fill_history_start=D(hist0),pf_fill_coverage=cover,
        pf_fill_coverage_pct=(cover*100 if cover is not None else None),
        pf_fills_in_completed_round_trips=covered_fill_count,pf_total_fills=nperp,slip_per_side=a.slip,
        pf_copy_full=pfc,pf_vault_full=full['pf_vault'],pf_copy_recent=rec_pfc,pf_vault_recent=rec['pf_vault'],
        pf_full=dict(full,pf_copy=pfc,first_close=D(full['first_close']),last_close=D(full['last_close'])),
        pf_recent=dict(rec,pf_copy=rec_pfc,first_close=D(rec['first_close']),last_close=D(rec['last_close'])),open_positions=sorted(open_tr),
        mdd_alltime=mdd_all,ret_alltime=tot_all,mdd_window=mdd_win,ret_window=tot_win,gate1_pass=gate1_pass,verdict=verdict,reasons=reasons,
        repro_check='MANUAL: Cove criterion 9 (reproducible on Railway, no 3rd-party keys) — answer Y/N before handing over')
    if a.json: print(json.dumps(res,indent=1,default=str));return
    f=lambda x,p='.2f': 'n/a' if x is None else format(x,p)
    print(f"{name or a.addr}  ({'vault' if is_vault else 'wallet'})  window {a.days}d, {len(eq)} equity pts, {len(F)} fills since {D(hist0)}, {len(FU)} funding recs")
    print(f"beta BTC {f(bb)} (R2 {f(r2b)}) | beta top20 EW {f(bkb)} (R2 {f(r2k)}) | beta share of PnL {f(beta_share,'.0%')} | net long time {f(net_long,'.0%')}")
    coverage_text='n/a' if cover is None else f'{cover:.1%} ({covered_fill_count}/{nperp} fills)'
    print(f'PF fill coverage: {coverage_text}')
    print(f"PF full history ({full['trips']} round trips {D(full['first_close'])}..{D(full['last_close'])}): COPY {f(pfc)} [gate >1.2] (net ${full['net_copy']:,.0f}) | vault-deposit ref {f(full['pf_vault'])} (net after 10% PS ${full['net_vault_after_ps']:,.0f})")
    print(f"  costs: closedPnl ${full['closedPnl']:,.0f}, actual fees ${full['fees']:,.0f}, copy fees ${full['copy_fees']:,.0f}, slip {a.slip:.2%}/side ${full['slip']:,.0f}, funding ${full['funding']:+,.0f}")
    print(f"PF last {a.days}d (reference only, {rec['trips']} trips): COPY {f(rec_pfc)} | vault-deposit {f(rec['pf_vault'])}")
    print(f"MDD allTime {mdd_all:.1%} (ret {tot_all:+.0%}) | MDD window {mdd_win:.1%} (ret {tot_win:+.0%})")
    print('VERDICT:',verdict,'|','; '.join(reasons) or 'no flags');print(res['repro_check'])
if __name__=='__main__': main()
