"""Enforces that the friction toolkit is never wired into a live portal adapter.

Wiring captcha-solving into a PortalAdapter.login() must be a new, explicit,
written decision (see docs/FRICTION_TOOLKIT.md and adapters/base.py's
login() docstring) - never a side effect of an import. This test is the
tripwire: it fails the moment any adapter module so much as mentions
"friction", long before such an import could actually run.
"""

from __future__ import annotations

from pathlib import Path

import shift_agent.adapters


def test_no_adapter_imports_the_friction_toolkit():
    adapters_dir = Path(shift_agent.adapters.__file__).parent
    offenders = [
        py.name for py in adapters_dir.glob("*.py") if "friction" in py.read_text(encoding="utf-8")
    ]
    assert not offenders, (
        f"{offenders} mention 'friction' - a PortalAdapter must never call into "
        "shift_agent.friction without a new, explicit, written decision "
        "recorded in docs/SECURITY.md and the adapter's own module docstring."
    )
