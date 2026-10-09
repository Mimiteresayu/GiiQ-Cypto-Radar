#!/usr/bin/env python3
"""Decide whether a Scout gate-1 run should open a GitHub issue.

Reads scout/out/gate.json (written by discover.py from screen.py's gate1_pass
flag). No network. The workflow's actions/github-script step is what talks to
the GitHub API with the built-in GITHUB_TOKEN.

  action "pass"  — at least one PASS candidate; body is the issue/comment text
  action "none"  — screen succeeded and nothing passed; open nothing
  action "stale" — screen failed, was cancelled, or left no usable output
"""
import argparse
import json
import math
import os
import sys
from datetime import datetime, timezone

PASS_LABEL = 'scout-pass'
STALE_LABEL = 'scout-stale'
LABELS = {
    PASS_LABEL: ('0E8A16', 'Gate-1 PASS candidates for Cove'),
    STALE_LABEL: ('D93F0B', 'Scout screen failed or produced no output'),
}


def is_gate_pass(candidate):
    """True when screen.py set gate1_pass and discover did not mark SUSPECT.

    Older screen JSON without gate1_pass falls back to a verdict that starts
    with PASS. SUSPECT rows are not handed to Cove.
    """
    if not isinstance(candidate, dict) or candidate.get('suspect'):
        return False
    screen = candidate.get('screen_result')
    if isinstance(screen, dict):
        if 'gate1_pass' in screen:
            return screen.get('gate1_pass') is True
        return str(screen.get('verdict') or '').startswith('PASS')
    if 'gate1_pass' in candidate:
        return candidate.get('gate1_pass') is True
    return False


def _num(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ('', 'n/a', 'none', 'null'):
            return None
        try:
            value = float(text)
        except ValueError:
            return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(value):
        return None
    return value


def _jsonable(value):
    number = _num(value)
    if number is not None and math.isinf(number):
        return 'inf' if number > 0 else '-inf'
    return value


def fmt_pf(value):
    number = _num(value)
    if number is None:
        return 'n/a'
    if math.isinf(number):
        return 'inf'
    return f'{number:.2f}'


def fmt_pct(value):
    number = _num(value)
    if number is None:
        return 'n/a'
    return f'{number * 100:.1f}%'


def fmt_tvl(value):
    number = _num(value)
    if number is None:
        return 'n/a'
    return f'${number:,.0f}'


def fmt_int(value):
    number = _num(value)
    if number is None:
        return 'n/a'
    return str(int(round(number)))


def slim_pass(candidate):
    """Fields the issue body needs, taken from the screen JSON."""
    screen = candidate.get('screen_result') or {}
    pf_full = screen.get('pf_full') or {}
    return {
        'name': candidate.get('name') or screen.get('name'),
        'address': candidate.get('address') or screen.get('address'),
        'tvl': _jsonable(candidate.get('tvl')),
        'return_90d': _jsonable(candidate.get('return_90d')),
        'gate1_pass': True,
        'pf_copy_full': _jsonable(screen.get('pf_copy_full')),
        'pf_copy_recent': _jsonable(screen.get('pf_copy_recent')),
        'mdd_alltime': _jsonable(screen.get('mdd_alltime')),
        'mdd_window': _jsonable(screen.get('mdd_window')),
        'beta_share_of_pnl': _jsonable(screen.get('beta_share_of_pnl')),
        'net_long_time': _jsonable(screen.get('net_long_time')),
        'pf_fill_coverage': _jsonable(screen.get('pf_fill_coverage')),
        'trips': _jsonable(pf_full.get('trips')),
    }


def write_gate(out_dir, data):
    """Write out/gate.json next to latest.md. Returns the path."""
    passes = [slim_pass(c) for c in (data.get('hl_vaults') or []) if is_gate_pass(c)]
    gate = {
        'date': data.get('date'),
        'pass_count': len(passes),
        'passes': passes,
    }
    path = os.path.join(out_dir, 'gate.json')
    with open(path, 'w') as fh:
        json.dump(gate, fh, indent=2)
        fh.write('\n')
    print(f'PASS candidates: {gate["pass_count"]}')
    return path


def load_gate(out_dir):
    """Return gate.json when latest.md and gate.json are both non-empty and valid."""
    latest = os.path.join(out_dir, 'latest.md')
    gate_path = os.path.join(out_dir, 'gate.json')
    if not os.path.isfile(latest) or os.path.getsize(latest) == 0:
        return None
    if not os.path.isfile(gate_path) or os.path.getsize(gate_path) == 0:
        return None
    try:
        with open(gate_path) as fh:
            gate = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(gate, dict) or not gate.get('date'):
        return None
    if 'passes' not in gate or 'pass_count' not in gate:
        return None
    return gate


def utc_today():
    return datetime.now(timezone.utc).strftime('%Y-%m-%d')


def report_url(server_url, repo):
    return f"{server_url.rstrip('/')}/{repo}/blob/scout-out/scout/out/latest.md"


def _clean(text, fallback):
    if text is None:
        return fallback
    cleaned = str(text).replace('\n', ' ').replace('\r', ' ').strip()
    return cleaned or fallback


def pass_title(date, count):
    noun = 'candidate' if count == 1 else 'candidates'
    return f'Scout PASS {date}: {count} {noun}'


def pass_title_prefix(date):
    return f'Scout PASS {date}:'


def format_pass_body(date, passes, server_url, repo, run_url):
    count = len(passes)
    noun = 'candidate' if count == 1 else 'candidates'
    lines = [
        f'Scout gate 1 found **{count}** PASS {noun} on {date}.',
        '',
        'Hand to Cove for IS/OOS and the month-block permutation test.',
        '',
    ]
    for index, row in enumerate(passes, start=1):
        name = _clean(row.get('name'), 'unnamed')
        address = _clean(row.get('address'), 'unknown')
        lines.append(f'{index}. **{name}** (`{address}`)')
        lines.append(f'   - Copy-route PF: {fmt_pf(row.get("pf_copy_full"))}')
        lines.append(f'   - Max drawdown: {fmt_pct(row.get("mdd_alltime"))}')
        lines.append(f'   - TVL: {fmt_tvl(row.get("tvl"))}')
        lines.append(f'   - Beta share of PnL: {fmt_pct(row.get("beta_share_of_pnl"))}')
        lines.append(f'   - Net long: {fmt_pct(row.get("net_long_time"))}')
        lines.append(f'   - 90d return: {fmt_pct(row.get("return_90d"))}')
        lines.append(f'   - PF fill coverage: {fmt_pct(row.get("pf_fill_coverage"))}')
        lines.append(f'   - Round trips: {fmt_int(row.get("trips"))}')
        lines.append(f'   - Window max drawdown: {fmt_pct(row.get("mdd_window"))}')
        lines.append(f'   - Copy PF last 90d (reference only): {fmt_pf(row.get("pf_copy_recent"))}')
        lines.append('')
    lines.append(f'Latest report (scout-out): {report_url(server_url, repo)}')
    lines.append(f'Actions run: {run_url}')
    return '\n'.join(lines).rstrip() + '\n'


def _with_label(payload, label):
    color, description = LABELS[label]
    payload['label'] = label
    payload['label_color'] = color
    payload['label_description'] = description
    return payload


def stale_payload(date, screen_status, has_output, run_url):
    if screen_status != 'success':
        reason = {
            'failure': 'screen failed',
            'cancelled': 'screen cancelled',
            'skipped': 'screen did not run',
        }.get(screen_status, f'screen {screen_status}')
    else:
        reason = 'no output'
    lines = [
        'The Scout gate-1 screen did not finish with a usable report. This issue is the signal so a miss is not silent.',
        '',
    ]
    if screen_status != 'success':
        lines.append(f'- Screen step status: `{screen_status}`')
    if not has_output:
        lines.append('- Usable output missing: need non-empty `scout/out/latest.md` and `scout/out/gate.json`.')
    lines.append('')
    lines.append(f'Actions run: {run_url}')
    return _with_label({
        'action': 'stale',
        'date': date,
        'title': f'Scout stale {date}: {reason}',
        'title_prefix': f'Scout stale {date}:',
        'body': '\n'.join(lines).rstrip() + '\n',
    }, STALE_LABEL)


def payload_from_gate(screen_status, gate, has_output, run_url, server_url, repo, today=None):
    """Pure decision. gate is the parsed gate.json or None when output is missing."""
    today = today or utc_today()
    if screen_status != 'success' or not has_output or gate is None:
        date = (gate or {}).get('date') or today
        return stale_payload(date, screen_status, bool(has_output and gate is not None), run_url)
    passes = [row for row in (gate.get('passes') or []) if is_gate_pass(row)]
    date = gate.get('date') or today
    if not passes:
        return {'action': 'none', 'date': date, 'pass_count': 0}
    count = len(passes)
    return _with_label({
        'action': 'pass',
        'date': date,
        'pass_count': count,
        'title': pass_title(date, count),
        'title_prefix': pass_title_prefix(date),
        'body': format_pass_body(date, passes, server_url, repo, run_url),
    }, PASS_LABEL)


def payload_for_discover(data, screen_status, repo, run_url, server_url='https://github.com', today=None):
    """Build a payload from an in-memory discover result (tests and dry runs)."""
    passes = [slim_pass(c) for c in (data.get('hl_vaults') or []) if is_gate_pass(c)]
    gate = {'date': data.get('date'), 'pass_count': len(passes), 'passes': passes}
    return payload_from_gate(
        screen_status=screen_status,
        gate=gate,
        has_output=bool(data.get('date')),
        run_url=run_url,
        server_url=server_url,
        repo=repo,
        today=today,
    )


def build_payload(out_dir, screen_status, repo, run_url, server_url='https://github.com', today=None):
    gate = load_gate(out_dir)
    return payload_from_gate(
        screen_status=screen_status,
        gate=gate,
        has_output=gate is not None,
        run_url=run_url,
        server_url=server_url,
        repo=repo,
        today=today,
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description='Print the Scout issue payload. Does not call GitHub.')
    parser.add_argument('--out-dir', required=True)
    parser.add_argument('--screen-status', required=True, help='success, failure, cancelled, or skipped')
    parser.add_argument('--repo', required=True, help='owner/name')
    parser.add_argument('--run-url', required=True)
    parser.add_argument('--server-url', default='https://github.com')
    parser.add_argument('--today', help='YYYY-MM-DD override when the screen wrote no date')
    parser.add_argument('--write', help='also write the payload JSON to this path')
    args = parser.parse_args(argv)
    try:
        payload = build_payload(
            out_dir=args.out_dir,
            screen_status=args.screen_status,
            repo=args.repo,
            run_url=args.run_url,
            server_url=args.server_url,
            today=args.today,
        )
    except Exception as exc:
        payload = stale_payload(args.today or utc_today(), 'failure', False, args.run_url)
        payload['body'] = f'notify.py could not build a payload ({exc}).\n\n' + payload['body']
    text = json.dumps(payload, indent=2) + '\n'
    if args.write:
        parent = os.path.dirname(args.write)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(args.write, 'w') as fh:
            fh.write(text)
    sys.stdout.write(text)
    return 0


if __name__ == '__main__':
    sys.exit(main())
