#Requires -Version 5.1
<#
.SYNOPSIS
    CrabDeck v2.2 Installer — Hermes + OpenClaw Edition
.DESCRIPTION
    Installs and configures the full CrabDeck stack:
      - Node.js (gateway + UI)
      - Python (orchestrator + Hermes + OpenClaw agents)
      - Ollama (Hermes LLM backend)
      - CrabDeck model (ollama/Modelfile)
    Generates a shared GATEWAY_TOKEN and writes it to every service's .env file,
    then places a desktop shortcut and Start-CrabDeck launcher.
.NOTES
    Run from the CrabDeck repo root:
        powershell -ExecutionPolicy Bypass -File installer\Install-CrabDeck.ps1
#>

param(
    [string]$InstallDir = "$env:USERPROFILE\CrabDeck",
    [switch]$SkipNodeCheck,
    [switch]$SkipPythonCheck,
    [switch]$SkipOllamaCheck,
    [switch]$NoShortcut,
    [switch]$OpenGateway   # set this only if you intentionally want NO auth (local dev only)
)

Set-StrictMode -Off
$ErrorActionPreference = 'Stop'

function Write-Step { param($m) Write-Host "`n  🦀  $m" -ForegroundColor Cyan }
function Write-OK   { param($m) Write-Host "  ✅  $m" -ForegroundColor Green }
function Write-Warn { param($m) Write-Host "  ⚠   $m" -ForegroundColor Yellow }
function Write-Fail { param($m) Write-Host "  ✗   $m" -ForegroundColor Red }
function Write-Info { param($m) Write-Host "      $m" -ForegroundColor Gray }

Write-Host ""
Write-Host "  ████████████████████████████████████████████" -ForegroundColor Cyan
Write-Host "  🦀  CRABDECK v2.2 INSTALLER" -ForegroundColor Cyan
Write-Host "      Hermes + OpenClaw Edition" -ForegroundColor DarkCyan
Write-Host "  ████████████████████████████████████████████" -ForegroundColor Cyan
Write-Host ""

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$SourceDir = Split-Path -Parent $ScriptDir   # one level up from installer/

# ── 1. Prerequisites check ─────────────────────────────────────────────────────
Write-Step "Checking prerequisites…"

if (-not $SkipNodeCheck) {
    try {
        $nodeVer = (node --version 2>&1).Trim()
        Write-OK "Node.js $nodeVer"
    } catch {
        Write-Fail "Node.js not found."
        Write-Info "Download from: https://nodejs.org/"
        exit 1
    }
}

if (-not $SkipPythonCheck) {
    try {
        $pyVer = (python --version 2>&1).Trim()
        Write-OK "$pyVer"
    } catch {
        Write-Fail "Python not found."
        Write-Info "Download from: https://www.python.org/downloads/"
        Write-Info "Make sure to check 'Add Python to PATH' during install."
        exit 1
    }
}

if (-not $SkipOllamaCheck) {
    try {
        $ollamaVer = (ollama --version 2>&1).Trim()
        Write-OK "Ollama $ollamaVer"
    } catch {
        Write-Warn "Ollama not found — Hermes LLM features will be unavailable."
        Write-Info "Install from: https://ollama.com/download"
        $global:OllamaMissing = $true
    }
}

# ── 2. Copy files to install directory ────────────────────────────────────────
Write-Step "Installing to $InstallDir …"
if (-not (Test-Path $InstallDir)) { New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null }
Copy-Item -Path "$SourceDir\*" -Destination $InstallDir -Recurse -Force
Write-OK "Files copied to $InstallDir"

# ── 3. npm install — Gateway ───────────────────────────────────────────────────
Write-Step "Installing Gateway dependencies (npm)…"
Push-Location "$InstallDir\gateway"
try {
    npm install --prefer-offline 2>&1 | ForEach-Object { Write-Info $_ }
    Write-OK "Gateway dependencies installed"
} catch { Write-Warn "npm install (gateway) failed: $_" } finally { Pop-Location }

# ── 4. npm install — UI ────────────────────────────────────────────────────────
Write-Step "Installing UI dependencies (npm)…"
Push-Location "$InstallDir\ui"
try {
    npm install --prefer-offline 2>&1 | ForEach-Object { Write-Info $_ }
    Write-OK "UI dependencies installed"
} catch { Write-Warn "npm install (ui) failed: $_" } finally { Pop-Location }

# ── 5. Python venv — Orchestrator ─────────────────────────────────────────────
Write-Step "Creating Python venv for Orchestrator…"
Push-Location "$InstallDir\orchestrator"
try {
    if (-not (Test-Path ".venv")) { python -m venv .venv 2>&1 | ForEach-Object { Write-Info $_ } }
    & ".venv\Scripts\python.exe" -m pip install -q -r requirements.txt 2>&1 | ForEach-Object { Write-Info $_ }
    Write-OK "Orchestrator venv ready"
} catch { Write-Warn "Orchestrator venv setup failed: $_" } finally { Pop-Location }

# ── 6. Python venv — Agents (Hermes + OpenClaw) ───────────────────────────────
Write-Step "Creating Python venv for Agents (Hermes + OpenClaw)…"
Push-Location "$InstallDir\agents"
try {
    if (-not (Test-Path ".venv")) { python -m venv .venv 2>&1 | ForEach-Object { Write-Info $_ } }
    & ".venv\Scripts\python.exe" -m pip install -q -r requirements.txt 2>&1 | ForEach-Object { Write-Info $_ }
    Write-OK "Agents venv ready (Hermes + OpenClaw)"
} catch { Write-Warn "Agents venv setup failed: $_" } finally { Pop-Location }

# ── 7. Build Ollama CrabDeck model ─────────────────────────────────────────────
if (-not $global:OllamaMissing) {
    Write-Step "Building Ollama 'crabdeck' model…"
    try {
        Write-Info "Pulling base model (llama3)…"
        ollama pull llama3 2>&1 | ForEach-Object { Write-Info $_ }
        Write-Info "Creating 'crabdeck' model from Modelfile…"
        ollama create crabdeck -f "$InstallDir\ollama\Modelfile" 2>&1 | ForEach-Object { Write-Info $_ }
        Write-OK "Ollama 'crabdeck' model built"
    } catch {
        Write-Warn "Ollama model build failed: $_ — run manually: ollama create crabdeck -f ollama\Modelfile"
    }
}

# ── 8. Generate shared GATEWAY_TOKEN and write .env files to every service ───
Write-Step "Generating gateway auth token and writing service configs…"

if ($OpenGateway) {
    $token = ""
    Write-Warn "Installed with -OpenGateway: the gateway will run WITHOUT auth. Localhost-only use intended."
} else {
    $bytes = New-Object byte[] 32
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $token = -join ($bytes | ForEach-Object { $_.ToString('x2') })
    Write-OK "Generated GATEWAY_TOKEN (stored in each service's .env, not printed to console)"
}

Set-Content -Path "$InstallDir\gateway\.env" -Value @"
HOST=0.0.0.0
PORT=8765
GATEWAY_TOKEN=$token
ALLOWED_ORIGINS=http://localhost:5173
"@

Set-Content -Path "$InstallDir\orchestrator\.env" -Value @"
GATEWAY_URL=ws://localhost:8765
GATEWAY_TOKEN=$token
ALLOWED_ORIGINS=http://localhost:5173
HOST=0.0.0.0
PORT=8000
"@

Set-Content -Path "$InstallDir\agents\.env" -Value @"
GATEWAY_URL=ws://localhost:8765
GATEWAY_TOKEN=$token
OLLAMA_URL=http://localhost:11434
DEFAULT_MODEL=llama3
OPENCLAW_HTTP=http://localhost:3131
ENABLE_SHELL_EXEC=0
SHELL_ALLOWLIST=
"@

Set-Content -Path "$InstallDir\ui\.env.local" -Value @"
VITE_GATEWAY_WS=ws://localhost:8765
VITE_OLLAMA_ENDPOINT=http://localhost:11434
VITE_ORCHESTRATOR=http://localhost:8000
VITE_GATEWAY_TOKEN=$token
"@

Write-OK ".env files written for gateway, orchestrator, agents, ui"
if (-not $OpenGateway) {
    Write-Info "OpenClaw shell execution is OFF by default. Set ENABLE_SHELL_EXEC=1 in agents\.env only after reading the security note at the top of agents\openclaw_agent.py."
}

# ── 9. Write launcher scripts (loads each .env before spawning the service) ──
Write-Step "Writing launcher scripts…"
$launcher = @"
# CrabDeck v2.2 — Full Stack Launcher (auto-generated by installer)
# Run: powershell -ExecutionPolicy Bypass -File Start-CrabDeck.ps1

function Import-DotEnv {
    param([string]`$Path)
    if (-not (Test-Path `$Path)) { return }
    Get-Content `$Path | ForEach-Object {
        if (`$_ -match '^\s*([^#=][^=]*)=(.*)$') {
            [System.Environment]::SetEnvironmentVariable(`$matches[1].Trim(), `$matches[2].Trim(), 'Process')
        }
    }
}

Set-Location '$InstallDir'
Write-Host '  🦀  CRABDECK v2.2 — Starting all services...' -ForegroundColor Cyan

# 1. Ollama
Write-Host '  [1/6] Starting Ollama...' -ForegroundColor DarkCyan
Start-Process powershell -ArgumentList '-NoExit', '-Command', 'ollama serve' -WindowStyle Minimized
Start-Sleep 2

# 2. Gateway
Import-DotEnv 'gateway\.env'
Write-Host '  [2/6] Starting Gateway (port 8765)...' -ForegroundColor DarkCyan
Start-Process powershell -ArgumentList '-NoExit', '-Command', 'cd gateway; node server.js' -WindowStyle Normal
Start-Sleep 1

# 3. Orchestrator
Import-DotEnv 'orchestrator\.env'
Write-Host '  [3/6] Starting Orchestrator (port 8000)...' -ForegroundColor DarkCyan
Start-Process powershell -ArgumentList '-NoExit', '-Command', 'cd orchestrator; .\.venv\Scripts\uvicorn main:app --host 0.0.0.0 --port 8000' -WindowStyle Minimized
Start-Sleep 1

# 4. Hermes Agent
Import-DotEnv 'agents\.env'
Write-Host '  [4/6] Starting Hermes Agent...' -ForegroundColor DarkCyan
Start-Process powershell -ArgumentList '-NoExit', '-Command', 'cd agents; .\.venv\Scripts\python hermes_agent.py' -WindowStyle Normal
Start-Sleep 1

# 5. OpenClaw Agent
Write-Host '  [5/6] Starting OpenClaw Agent...' -ForegroundColor DarkCyan
Start-Process powershell -ArgumentList '-NoExit', '-Command', 'cd agents; .\.venv\Scripts\python openclaw_agent.py' -WindowStyle Normal
Start-Sleep 2

# 6. UI (opens browser)
Write-Host '  [6/6] Starting UI (port 5173)...' -ForegroundColor DarkCyan
Start-Process powershell -ArgumentList '-NoExit', '-Command', 'cd ui; npm run dev' -WindowStyle Minimized
Start-Sleep 3
Start-Process 'http://localhost:5173'

Write-Host ''
Write-Host '  ✅  CrabDeck v2.2 is running!' -ForegroundColor Green
Write-Host '      UI           → http://localhost:5173' -ForegroundColor Cyan
Write-Host '      Gateway      → ws://localhost:8765' -ForegroundColor Cyan
Write-Host '      Orchestrator → http://localhost:8000' -ForegroundColor Cyan
Write-Host ''
"@
Set-Content -Path "$InstallDir\Start-CrabDeck.ps1" -Value $launcher

$stopper = @"
# CrabDeck Stop Script (auto-generated)
Write-Host 'Stopping CrabDeck services...' -ForegroundColor Yellow
Get-Process -Name 'node'    -ErrorAction SilentlyContinue | Stop-Process -Force
Get-Process -Name 'uvicorn' -ErrorAction SilentlyContinue | Stop-Process -Force
Get-Process -Name 'python'  -ErrorAction SilentlyContinue | Stop-Process -Force
Write-Host 'All CrabDeck services stopped.' -ForegroundColor Green
"@
Set-Content -Path "$InstallDir\Stop-CrabDeck.ps1" -Value $stopper
Write-OK "Launcher scripts written"

# ── 10. Desktop shortcut ────────────────────────────────────────────────────────
if (-not $NoShortcut) {
    Write-Step "Creating desktop shortcut…"
    try {
        $desktop = [Environment]::GetFolderPath('Desktop')
        $wsh     = New-Object -ComObject WScript.Shell
        $lnk     = $wsh.CreateShortcut("$desktop\CrabDeck.lnk")
        $lnk.TargetPath       = 'powershell.exe'
        $lnk.Arguments        = "-ExecutionPolicy Bypass -File `"$InstallDir\Start-CrabDeck.ps1`""
        $lnk.WorkingDirectory = $InstallDir
        $lnk.Description      = 'Launch CrabDeck v2.2 — Hermes + OpenClaw Edition'
        $lnk.IconLocation     = 'powershell.exe'
        $lnk.Save()
        Write-OK "Desktop shortcut created: CrabDeck.lnk"
    } catch { Write-Warn "Could not create shortcut: $_" }
}

# ── Done ───────────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "  ████████████████████████████████████████████" -ForegroundColor Green
Write-Host "  ✅  CrabDeck v2.2 installed successfully!" -ForegroundColor Green
Write-Host ""
Write-Host "  To launch everything at once:" -ForegroundColor Cyan
Write-Host "    powershell -ExecutionPolicy Bypass -File `"$InstallDir\Start-CrabDeck.ps1`"" -ForegroundColor White
Write-Host ""
Write-Host "  Or use the 'CrabDeck' shortcut on your Desktop." -ForegroundColor White
Write-Host ""
Write-Host "  Services:" -ForegroundColor DarkCyan
Write-Host "    UI          → http://localhost:5173" -ForegroundColor Gray
Write-Host "    Gateway     → ws://localhost:8765" -ForegroundColor Gray
Write-Host "    Orchestrator→ http://localhost:8000" -ForegroundColor Gray
Write-Host "    Hermes      → python agents/hermes_agent.py" -ForegroundColor Gray
Write-Host "    OpenClaw    → python agents/openclaw_agent.py" -ForegroundColor Gray
Write-Host ""
Write-Host "  Before exposing this on hermes-claw.ai, read SECURITY.md." -ForegroundColor Yellow
Write-Host "  ████████████████████████████████████████████" -ForegroundColor Green
Write-Host ""
