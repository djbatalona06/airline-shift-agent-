"""PyInstaller entry point.

A module cannot be bundled with `-m`, so the frozen build needs a real script.
Importing `main` here also gives PyInstaller a static reference to the adapter
registrations in `shift_agent.main`, which the decorator-based registry would
otherwise hide from its analysis.
"""

from __future__ import annotations

import multiprocessing
import sys

from shift_agent.main import main

if __name__ == "__main__":
    # Required in frozen builds: without it, any child process re-executes the
    # bundle from the top and forks endlessly.
    multiprocessing.freeze_support()
    sys.exit(main())
