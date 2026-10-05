#!/usr/bin/env python3
"""Apply the C48-3 patches to the pinned upstream checkout. Exit 0 = all applied.

Each patch is an exact-text replacement whose anchor must occur exactly once, so any
upstream drift fails the Docker build instead of silently skipping a lock.

  B1  enhanced_config.py: lower max_drawdown_pct / max_position_size_pct minimums to 1.0
      (upstream 5.0 / 10.0 reject the gate values DD=4, pos=8).
  B2  run_bot.py::_convert_config: pass risk_management to the engine (upstream drops it,
      so RiskManager runs on code defaults: SL off, DD 15%, pos 30%).
  B3  run_bot.py::_convert_config: size allocation from GIIQ_NAV_USD (real testnet equity,
      set by giiq_entrypoint.py) instead of the hardcoded $1000; refuse exchange.testnet != true.

Usage: python3 apply_patches.py <upstream_dir>
"""
from __future__ import annotations

import sys
from pathlib import Path

PATCHES: list[tuple[str, str, str, str]] = [
    (
        "B1",
        "src/core/enhanced_config.py",
        """        if not 5.0 <= self.max_drawdown_pct <= 50.0:
            raise ValueError("max_drawdown_pct must be between 5.0 and 50.0")
        if not 10.0 <= self.max_position_size_pct <= 100.0:
            raise ValueError("max_position_size_pct must be between 10.0 and 100.0")
""",
        """        if not 1.0 <= self.max_drawdown_pct <= 50.0:
            raise ValueError("max_drawdown_pct must be between 1.0 and 50.0")
        if not 1.0 <= self.max_position_size_pct <= 100.0:
            raise ValueError("max_position_size_pct must be between 1.0 and 100.0")
""",
    ),
    (
        "B3",
        "src/run_bot.py",
        """        testnet = os.getenv("HYPERLIQUID_TESTNET", "true").lower() == "true"

        # Calculate total allocation in USD from account balance percentage
        # Note: This is a simplified approach - in production, you'd get actual account balance
        # For now, using a default base amount of $1000 USD
        base_allocation_usd = 1000.0
""",
        """        if self.config.exchange.testnet is not True:
            raise ValueError("C48-3: exchange.testnet must be true (testnet only)")

        nav_raw = os.getenv("GIIQ_NAV_USD", "").strip()
        if not nav_raw:
            raise ValueError("C48-3: GIIQ_NAV_USD (real testnet equity) is required")
        base_allocation_usd = float(nav_raw)
        if not base_allocation_usd > 0:
            raise ValueError(f"C48-3: GIIQ_NAV_USD must be > 0, got {nav_raw!r}")
        rm = self.config.risk_management
""",
    ),
    (
        "B2",
        "src/run_bot.py",
        """            "log_level": self.config.monitoring.log_level,
        }
""",
        """            "risk_management": {
                "stop_loss_enabled": rm.stop_loss_enabled,
                "stop_loss_pct": rm.stop_loss_pct,
                "take_profit_enabled": rm.take_profit_enabled,
                "take_profit_pct": rm.take_profit_pct,
                "tpsl_mode": rm.tpsl_mode,
                "max_drawdown_pct": rm.max_drawdown_pct,
                "max_position_size_pct": rm.max_position_size_pct,
            },
            "log_level": self.config.monitoring.log_level,
        }
""",
    ),
]


def apply(upstream: Path) -> list[str]:
    """All-or-nothing: nothing is written unless every patch applies."""
    errors: list[str] = []
    texts: dict[str, str] = {}
    for tag, rel, old, new in PATCHES:
        text = texts.get(rel) or (upstream / rel).read_text(encoding="utf-8")
        if new in text:
            errors.append(f"{tag}: already applied to {rel}")
            continue
        count = text.count(old)
        if count != 1:
            errors.append(f"{tag}: anchor in {rel} found {count}x (want 1)")
            continue
        texts[rel] = text.replace(old, new)
    if errors:
        return errors
    for rel, text in texts.items():
        (upstream / rel).write_text(text, encoding="utf-8")
    for tag, rel, _, _ in PATCHES:
        print(f"{tag}: patched {rel}")
    return []


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    errors = apply(Path(argv[0]))
    if errors:
        print("PATCH FAILED: " + "; ".join(errors), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
