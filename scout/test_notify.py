#!/usr/bin/env python3
"""Dry run of the Scout PASS / no-PASS / stale issue decision. No network."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import discover
import notify

ROOT = Path(__file__).resolve().parent
REPO = 'acme/radar'
RUN = 'https://github.com/acme/radar/actions/runs/99'
SERVER = 'https://github.com'
REPORT = 'https://github.com/acme/radar/blob/scout-out/scout/out/latest.md'


def _pass_candidate(name, address, pf):
    return {
        'name': name,
        'address': address,
        'tvl': 2500000,
        'return_90d': 0.35,
        'mdd': 0.08,
        'suspect': False,
        'screen_result': {
            'gate1_pass': True,
            'verdict': 'PASS gate1 -> hand to Cove (IS/OOS + permutation)',
            'pf_copy_full': pf,
            'pf_copy_recent': 1.62,
            'mdd_alltime': 0.18,
            'mdd_window': 0.04,
            'beta_share_of_pnl': 0.22,
            'net_long_time': 0.41,
            'pf_fill_coverage': 0.91,
            'pf_full': {'trips': 140},
        },
    }


def _discover_data(vaults, date='2026-10-06'):
    return {
        'date': date,
        'hl_vaults': vaults,
        'funding_spreads': [],
        'github_bots': [],
        'filter_cuts': {'tvl': 0, 'age': 0, 'return': 0, 'mdd': 0, 'rejected': 0, 'no_data': 0},
        'errors': [],
    }


class NotifyTests(unittest.TestCase):
    def test_pass_builds_issue_body(self):
        data = _discover_data([
            _pass_candidate('Northwind Carry', '0x1111111111111111111111111111111111111111', 1.48),
            _pass_candidate('Harbor Basis', '0x2222222222222222222222222222222222222222', 1.71),
        ])
        payload = notify.payload_for_discover(
            data, screen_status='success', repo=REPO, run_url=RUN, server_url=SERVER,
        )
        self.assertEqual(payload['action'], 'pass')
        self.assertEqual(payload['label'], 'scout-pass')
        self.assertEqual(payload['pass_count'], 2)
        self.assertEqual(payload['title'], 'Scout PASS 2026-10-06: 2 candidates')
        self.assertEqual(payload['title_prefix'], 'Scout PASS 2026-10-06:')
        body = payload['body']
        self.assertIn('Northwind Carry', body)
        self.assertIn('0x1111111111111111111111111111111111111111', body)
        self.assertIn('Copy-route PF: 1.48', body)
        self.assertIn('Copy-route PF: 1.71', body)
        self.assertIn('Max drawdown: 18.0%', body)
        self.assertIn('TVL: $2,500,000', body)
        self.assertIn('Beta share of PnL: 22.0%', body)
        self.assertIn('Net long: 41.0%', body)
        self.assertIn('90d return: 35.0%', body)
        self.assertIn('PF fill coverage: 91.0%', body)
        self.assertIn('Round trips: 140', body)
        self.assertIn(REPORT, body)
        self.assertIn(RUN, body)
        self.assertNotIn('SUSPECT', body)

    def test_single_pass_uses_singular_title(self):
        data = _discover_data([
            _pass_candidate('Northwind Carry', '0xabc', 1.48),
        ])
        payload = notify.payload_for_discover(
            data, screen_status='success', repo=REPO, run_url=RUN, server_url=SERVER,
        )
        self.assertEqual(payload['title'], 'Scout PASS 2026-10-06: 1 candidate')

    def test_no_pass_does_nothing(self):
        data = json.loads((ROOT / 'out' / '2026-10-05.json').read_text())
        payload = notify.payload_for_discover(
            data, screen_status='success', repo=REPO, run_url=RUN, server_url=SERVER,
        )
        self.assertEqual(payload['action'], 'none')
        self.assertEqual(payload['pass_count'], 0)
        self.assertNotIn('title', payload)
        self.assertNotIn('body', payload)
        self.assertNotIn('label', payload)

    def test_suspect_is_not_a_pass(self):
        row = _pass_candidate('Too Good', '0x' + 'ab' * 20, 20)
        row['suspect'] = True
        row['suspect_reason'] = 'copy PF 20.00 >10'
        data = _discover_data([row])
        payload = notify.payload_for_discover(
            data, screen_status='success', repo=REPO, run_url=RUN, server_url=SERVER,
        )
        self.assertEqual(payload['action'], 'none')
        summary = discover.generate_summary(data)
        self.assertNotIn('## PASS Gate 1', summary)
        self.assertIn('No candidates passed gate 1 today', summary)
        self.assertIn('SUSPECT', summary)

    def test_gate1_pass_false_beats_verdict_string(self):
        row = _pass_candidate('Mismatch', '0xabc', 1.5)
        row['screen_result']['gate1_pass'] = False
        row['screen_result']['verdict'] = 'PASS gate1 -> hand to Cove (IS/OOS + permutation)'
        self.assertFalse(notify.is_gate_pass(row))

    def test_legacy_verdict_prefix_still_counts(self):
        row = _pass_candidate('Legacy', '0xabc', 1.5)
        del row['screen_result']['gate1_pass']
        self.assertTrue(notify.is_gate_pass(row))

    def test_failed_screen_is_stale_even_with_passes(self):
        data = _discover_data([
            _pass_candidate('Northwind Carry', '0xabc', 1.48),
        ])
        payload = notify.payload_for_discover(
            data, screen_status='failure', repo=REPO, run_url=RUN, server_url=SERVER,
        )
        self.assertEqual(payload['action'], 'stale')
        self.assertEqual(payload['label'], 'scout-stale')
        self.assertTrue(payload['title'].startswith('Scout stale 2026-10-06:'))
        self.assertIn('screen failed', payload['title'])
        self.assertIn(RUN, payload['body'])
        self.assertNotIn('Northwind Carry', payload['body'])

    def test_missing_output_is_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = notify.build_payload(
                tmp, screen_status='success', repo=REPO, run_url=RUN, server_url=SERVER, today='2026-10-06',
            )
        self.assertEqual(payload['action'], 'stale')
        self.assertIn('no output', payload['title'])
        self.assertIn('gate.json', payload['body'])

    def test_cli_pass_and_no_pass(self):
        script = str(ROOT / 'notify.py')
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            pass_dir = out / 'pass'
            none_dir = out / 'none'
            pass_dir.mkdir()
            none_dir.mkdir()
            (pass_dir / 'latest.md').write_text('# report\n')
            (none_dir / 'latest.md').write_text('# report\n')
            notify.write_gate(str(pass_dir), _discover_data([
                _pass_candidate('Northwind Carry', '0x1111111111111111111111111111111111111111', 1.48),
            ]))
            none_data = json.loads((ROOT / 'out' / '2026-10-05.json').read_text())
            notify.write_gate(str(none_dir), none_data)

            pass_payload = _run_cli(script, pass_dir)
            none_payload = _run_cli(script, none_dir)

        self.assertEqual(pass_payload['action'], 'pass')
        self.assertIn('Northwind Carry', pass_payload['body'])
        self.assertIn('Copy-route PF: 1.48', pass_payload['body'])
        self.assertEqual(none_payload['action'], 'none')
        self.assertNotIn('body', none_payload)


def _run_cli(script, out_dir):
    proc = subprocess.run(
        [sys.executable, script,
         '--out-dir', str(out_dir),
         '--screen-status', 'success',
         '--repo', REPO,
         '--run-url', RUN,
         '--server-url', SERVER,
         '--today', '2026-10-06'],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(proc.stdout)


class WorkflowContractTests(unittest.TestCase):
    def test_workflow_uses_github_token_issues_write_only(self):
        text = (ROOT.parent / '.github' / 'workflows' / 'scout_screen.yml').read_text()
        self.assertIn('issues: write', text)
        self.assertIn('dry_run', text)
        self.assertIn('scout/notify.py', text)
        self.assertIn('scout/open_issue.js', text)
        self.assertNotIn('secrets.', text.replace('secrets.GITHUB_TOKEN', ''))
        self.assertNotIn('pull-requests:', text)


if __name__ == '__main__':
    unittest.main()
