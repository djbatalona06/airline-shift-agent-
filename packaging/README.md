# Packaging findings — Phase 0 spike

Run on Windows 11, Python 3.14.5, PyInstaller 6.22.0, pywebview 6.2.1,
pythonnet 3.1.0. Verdict: **single portable `.exe` is viable.**

## Results

| Check | Result |
|---|---|
| Builds with `--onefile` | Yes |
| `keyring` works in the **frozen** build | Yes — `WinVaultKeyring`, round-trip verified |
| `pywebview` / `pythonnet` import when frozen | Yes |
| WebView2 window opens from the bundle | Yes, opened and closed cleanly |
| Defender quarantine | None |
| Size | 13.7 MB (no Chromium) |
| Startup — console checks | ~2.2 s warm, ~2.8 s cold |
| Startup — to visible window | ~5–6 s (first WebView2 init dominates) |

Startup is paid once for a process that then stays resident, so it is fine here.
It would not be acceptable for something invoked per-poll.

## Required hidden imports

`--onefile` resolves imports by static analysis. Anything imported dynamically is
invisible and fails **only in the packaged build**, which is the worst place to
discover it.

```
--hidden-import keyring.backends.Windows
--hidden-import clr_loader
```

The spike proved this empirically: `httpx` and `pydantic` were reported missing
in the frozen build purely because `spike.py` reached them through
`__import__(name)` in a loop. Adding them as hidden imports fixed it. They need
no hidden import in the real app, which imports them statically.

## The friction toolkit's dependency (`anthropic`)

`anthropic` is a base dependency now (`src/shift_agent/friction/`), reached
by `main.py`'s `friction-*` subcommands through the same kind of lazy,
inside-a-function import already used for `_dashboard`/`_recon` — PyInstaller's
static analysis parses whole files, including function bodies, so this
should need no extra `--hidden-import` entry, unlike the adapter registry
below. Still: **smoke-test `ShiftAgent.exe friction-bench --help` before
shipping a release.** If the `anthropic` SDK does its own dynamic imports
internally and PyInstaller misses one, the fix is an additional
`--hidden-import` in `build_release.ps1`, not a change to `friction/` itself.
Expect the zip to grow modestly versus the 13.7 MB baseline below —
`anthropic` and its light dependency set (`distro`, `jiter`, `sniffio`,
`docstring_parser`) are now bundled.

## The trap for the real build

`adapters/base.py` resolves adapters through a **registry populated by
decorators** — `get_adapter("flica")` looks up a dict, so PyInstaller sees no
reference to the module.

`main.py` already handles this:

```python
from .adapters import mock as _mock  # noqa: F401  -- registers the "mock" adapter
```

**That import looks unused and is not.** Deleting it, or letting a linter
auto-remove it, produces an `.exe` that builds cleanly and then fails at runtime
with "unknown adapter". Every adapter must be statically imported there — add
`flica` the same way when it exists.

## Real-app build differences

- Use `--windowed` (no console). The spike kept the console to read JSON output.
- Bundle `dashboard/template.html` as a data file; resolve it via `sys._MEIPASS`
  (the spike confirmed `_MEIPASS` is populated).
- **Chromium ships beside the exe, not inside it.** Playwright's browser is
  ~150 MB and `--onefile` unpacks its whole payload to temp on every launch.
  Point `PLAYWRIGHT_BROWSERS_PATH` at the external folder.
- Whether Chromium is needed at runtime at all depends on the recon cookie
  result. FLICA returns 403 to non-browser requests, so plan for needing it.

## Reproducing

```bash
python -m PyInstaller --onefile --name spike --distpath packaging/dist --workpath packaging/build --specpath packaging --hidden-import keyring.backends.Windows --hidden-import clr_loader --noconfirm packaging/spike.py
packaging/dist/spike.exe --checks
packaging/dist/spike.exe --window --seconds 4
```

`spike.py` is throwaway and not shipped. Keep it until the real spec file exists.
