"""Opt-in, portal-agnostic toolkit for handling web-auth friction.

Screenshot -> vision model -> structured action, plus IMAP one-time-code
reading. It ships as part of the app (see `main.py`'s `friction-*`
subcommands) but nothing here is imported by `adapters/`, and nothing here
imports from `adapters/`. See docs/FRICTION_TOOLKIT.md for why, and
`tests/test_friction_boundary.py` for the regression guard.
"""

from __future__ import annotations
