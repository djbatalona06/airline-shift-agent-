# Deploying a release

How `ShiftAgent-windows.zip` gets built and attached to the
[GitHub Release](https://github.com/djbatalona06/airline-shift-agent-/releases/tag/release),
and why the one published on 2026-08-12 didn't work.

## What went wrong with v1.0.1

The published zip had `ShiftAgent.exe`, `INSTALL.md`, and `READ ME FIRST.txt`
— no `browsers/` folder. `main.py` only ever looks for Chromium beside the
exe, so every run hit Playwright's own "run `playwright install chromium`"
message, which means nothing to someone who only has an `.exe`.

Two separate bugs, now both fixed:

1. **The zip itself.** `packaging/build_release.ps1` *does* stage the full
   Chromium build into `browsers/` and throws if it can't find one to copy —
   so the shipped zip was built some other way: hand-zipped from
   `packaging/dist/` (bare exe only) instead of the staged
   `packaging/release/ShiftAgent/` folder the script produces, or built with
   an older copy of the script. Either way, nothing checked the *finished
   zip* before it was uploaded. The script now unzips its own output and
   throws if `ShiftAgent.exe` or a `browsers/chromium*/` payload is missing,
   so a zip built by this script can no longer ship without them.
2. **The failure was silent.** `browsers_available()` in `main.py` decided
   "browsers are fine" by checking whether `PLAYWRIGHT_BROWSERS_PATH` was
   set — but that variable is only set *when the browsers folder was found*,
   so a missing folder made the check pass instead of fail. It now checks
   the frozen build's own `browsers/` folder directly, so a bad zip prints
   the intended "re-extract the whole zip" message instead of Playwright's
   generic one.

`packaging/release/` is gitignored on purpose — the zip is a ~200 MB binary
blob that doesn't belong in version control — which is also why nothing
about its contents was ever CI-checked before now.

## Cutting a release (recommended: GitHub Actions, no Windows needed)

`.github/workflows/release.yml` builds on a clean `windows-latest` runner and
uploads the result, so you don't need a Windows machine or a local Python
environment:

1. Go to **Actions → release → Run workflow** on this repository.
2. Enter the tag to attach the zip to (defaults to `release`, the existing
   one — nothing about the tag or release notes changes, only the asset).
3. Run it. It installs the project, installs Chromium, runs the same
   `packaging/build_release.ps1` a human would, and uploads
   `ShiftAgent-windows.zip` to that release with `gh release upload --clobber`
   (replaces the existing asset of the same name).

The workflow file has to exist on a branch GitHub already knows about
(typically `main`) before it shows up under **Run workflow** — merge the PR
that adds it first.

## Cutting a release manually (Windows machine)

Same script, run by hand — useful if you don't want to touch CI, or you're
iterating on the script itself.

```powershell
git clone https://github.com/djbatalona06/airline-shift-agent-.git
cd airline-shift-agent-
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev,packaging]"
.venv\Scripts\python.exe -m playwright install chromium

pwsh -File packaging/build_release.ps1
```

That runs the test suite, builds `ShiftAgent.exe` with PyInstaller, stages it
next to the Chromium build Playwright just installed, zips
`packaging/release/ShiftAgent-windows.zip`, and now also verifies the zip
contains both before it finishes. If it throws, don't upload — read the
error, it names exactly what's missing.

Upload it as a **Release asset**, never commit it:

```bash
gh release upload release packaging/release/ShiftAgent-windows.zip --clobber
```

Or through the GitHub UI: **Releases → release → Edit → drag the zip in**,
replacing the old asset.

## Verifying an asset before (or after) it ships

Whether it came from CI or a manual build, a 30-second sanity check before
telling anyone to download it:

```bash
unzip -l ShiftAgent-windows.zip
```

Expect to see, alongside `ShiftAgent.exe`:

```
ShiftAgent/browsers/chromium-<some number>/...
```

No `browsers/` entry at all is exactly the v1.0.1 bug. If you have Windows
handy, the real test is running it: unzip, double-click `ShiftAgent.exe`,
finish the setup window, and confirm a poll cycle runs without the "browser
files are missing" message.

## Versioning note

The repository has one release, tagged `release` (not semver). Re-running
the workflow above against that same tag replaces its asset in place, which
is enough for now — moving to versioned tags (`v1.0.2`, …) is a separate,
later decision and not required to ship a working zip today.
