# Build the shippable Windows release: ShiftAgent.exe plus the Chromium it needs.
#
# Chromium travels BESIDE the exe rather than inside it. --onefile unpacks its
# entire payload to a temp folder on every launch, and doing that with a 150 MB
# browser would make startup unbearable. main._configure_browsers() points
# Playwright at the sibling folder at runtime.
#
#   pwsh -File packaging/build_release.ps1

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root ".venv\Scripts\python.exe"
$dist = Join-Path $root "packaging\dist"
$stage = Join-Path $root "packaging\release\ShiftAgent"

if (-not (Test-Path $py)) { throw "No virtualenv at $py. See docs/INSTALL.md." }

Write-Host "==> Tests" -ForegroundColor Cyan
& $py -m pytest -q
if ($LASTEXITCODE -ne 0) { throw "Tests failed - not building a release." }

Write-Host "==> Building executable" -ForegroundColor Cyan
$template = Join-Path $root "src\shift_agent\dashboard\template.html"
$setupTemplate = Join-Path $root "src\shift_agent\setup\index.html"
# --windowed: the app now has a real first-run setup window (see
# src/shift_agent/setup/) and a message-box crash path (packaging/entry.py)
# to replace it, so a working build never needs a console. See
# packaging/README.md for why the earlier build skipped this flag.
& $py -m PyInstaller --onefile --windowed --name ShiftAgent `
    --distpath $dist `
    --workpath (Join-Path $root "packaging\build") `
    --specpath (Join-Path $root "packaging") `
    --paths (Join-Path $root "src") `
    --hidden-import keyring.backends.Windows `
    --hidden-import clr_loader `
    --add-data "$template;shift_agent/dashboard" `
    --add-data "$setupTemplate;shift_agent/setup" `
    --noconfirm (Join-Path $root "packaging\entry.py")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed." }

Write-Host "==> Staging" -ForegroundColor Cyan
if (Test-Path $stage) { Remove-Item -LiteralPath $stage -Recurse -Force }
New-Item -ItemType Directory -Force -Path $stage | Out-Null
Copy-Item (Join-Path $dist "ShiftAgent.exe") $stage

# Copy only the full Chromium build. chromium_headless_shell cannot show a
# window, and this portal needs a human to clear its captcha.
$browserRoot = Join-Path $env:LOCALAPPDATA "ms-playwright"
$chromium = Get-ChildItem $browserRoot -Directory -Filter "chromium-*" -ErrorAction SilentlyContinue |
    Sort-Object Name -Descending | Select-Object -First 1
if (-not $chromium) { throw "No Chromium found. Run: $py -m playwright install chromium" }

$target = Join-Path $stage "browsers"
New-Item -ItemType Directory -Force -Path $target | Out-Null
Copy-Item $chromium.FullName -Destination $target -Recurse
Write-Host "    bundled $($chromium.Name)"

Copy-Item (Join-Path $root "docs\INSTALL.md") (Join-Path $stage "INSTALL.md")
@"
Shift Agent

1. Keep this whole folder together. Moving ShiftAgent.exe on its own
   will break it - it needs the 'browsers' folder beside it.
2. Double-click ShiftAgent.exe.
3. If Windows warns the app is unrecognised, click "More info" then
   "Run anyway". It is unsigned because code-signing certificates cost
   money; it is not a virus warning.

Full instructions are in INSTALL.md.
"@ | Set-Content (Join-Path $stage "READ ME FIRST.txt") -Encoding UTF8

Write-Host "==> Zipping" -ForegroundColor Cyan
$zip = Join-Path $root "packaging\release\ShiftAgent-windows.zip"
if (Test-Path $zip) { Remove-Item $zip -Force }
Compress-Archive -Path $stage -DestinationPath $zip

$mb = [math]::Round((Get-Item $zip).Length / 1MB, 1)
Write-Host "==> Done: $zip ($mb MB)" -ForegroundColor Green
Write-Host "    Upload as a GitHub Release asset - do not commit it."
