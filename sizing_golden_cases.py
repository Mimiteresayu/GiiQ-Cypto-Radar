"""Golden SoT-4 sizing cases. Regenerate test_fixtures/sizing_sot4_golden.json (only from the pre-change main code)."""
import itertools
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import exec_common as ec  # noqa: E402

CASES = []
for nav, (entry, sl), cm, ai_sz, ai_lev, tier in itertools.product(
        (1000.0, 25000.0),
        ((100.0, 98.0), (100.0, 90.0), (100.0, 80.0), (100.0, 72.0), (100.0, 70.0), (0.5123, 0.47)),
        (None, 2, 3, 5, 10, 50),
        (None, 1, 3, 6),
        (None, 2, 4, 9),
        (None, "mega", "small", "tiny", "")):
    CASES.append({"nav": nav, "entry_px": entry, "hard_sl": sl, "coin_max_leverage": cm,
                  "ai_size_pct": ai_sz, "ai_leverage": ai_lev, "tier": tier})
for fixed, room, ref, tier in itertools.product((2, 3, 5), (None, 1.5, 2.5, 5.5), (None, 110.0), (None, "large", "tiny")):
    CASES.append({"nav": 1000.0, "entry_px": 100.0, "hard_sl": 90.0, "coin_max_leverage": 10,
                  "fixed_leverage": fixed, "max_margin_pct": room, "liq_ref_px": ref, "tier": tier})
CASES.append({"nav": 0, "entry_px": 100.0, "hard_sl": 90.0, "coin_max_leverage": 10})
CASES.append({"nav": 1000.0, "entry_px": 100.0, "hard_sl": 101.0, "coin_max_leverage": 10})


def run_all():
    return [{"in": c, "out": ec.size_by_margin(**c)} for c in CASES]


if __name__ == "__main__":
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_fixtures", "sizing_sot4_golden.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(run_all(), fh, sort_keys=True, indent=0)
    print(out, len(CASES))
