# ============================================================
#  Start-CrabDeck.ps1  (repo-root dev launcher)
#  For running straight out of a cloned repo without the full
#  installer. The installer (installer/Install-CrabDeck.ps1)
#  writes its OWN copy of this script into the install dir with
#  paths baked in — this version uses $PSScriptRoot for relative
#  paths so it also works for local development.
#
#  Starts: Ollama → Gateway → Orchestrator → Hermes/OpenClaw → UI
# ============================================================

param(
    [string]$Root = "$PSScriptRoot",
    [switch]$GatewayOnly,
    [switch]$UIOnly,
    [switch]$NoOllama,
    [switch]$NoBrowser
)

$ErrorActionPreference = "SilentlyContinue"

function OK   { param($m) Write-Host "  [OK]  $m" -ForegroundColor Green  }
function WARN { param($m) Write-Host "  [!!]  $m" -ForegroundColor Yellow }
function STEP { param($m) Write-Host "`n[>] $m"   -ForegroundColor Cyan   }

function Import-DotEnv {
    param([string]$Path)
    if (-not (Test-Path $Path)) { return }
    Get-Content $Path | ForEach-Object {
        if ($_ -match '^\s*([^#=][^=]*)=(.*)$') {
            [System.Environment]::SetEnvironmentVariable($matches[1].Trim(), $matches[2].Trim(), 'Process')
        }
    }
}

Write-Host @"

  ██████╗██████╗  █████╗ ██████╗ ██████╗ ███████╗ ██████╗██╗  ██╗
 ██╔════╝██╔══██╗██╔══██╗██╔══██╗██╔══██╗██╔════╝██╔════╝██║ ██╔╝
 ██║     ██████╔╝███████║██████╔╝██║  ██║█████╗  ██║     █████╔╝
 ██║     ██╔══██╗██╔══██║██╔══██╗██║  ██║██╔══╝  ██║     ██╔═██╗
 ╚██████╗██║  ██║██║  ██║██████╔╝██████╔╝███████╗╚██████╗██║  ██╗
  ╚═════╝╚═╝  ╚═╝╚═╝  ╚═╝╚═════╝ ╚═════╝ ╚══════╝ ╚═════╝╚═╝  ╚═╝
  v2.2  ·  Hermes + OpenClaw Edition  ·  Team CrabDeck

"@ -ForegroundColor Cyan

# ── 1. Ollama ─────────────────────────────────────────────────────────────────
if (-not $NoOllama) {
    STEP "Checking Ollama"
    $ollama = Get-Process "ollama" -ErrorAction SilentlyContinue
    if (-not $ollama) {
        Start-Process "ollama" -ArgumentList "serve" -WindowStyle Minimized
        Start-Sleep -Seconds 2
        OK "Ollama started  → http://localhost:11434"
    } else {
        OK "Ollama already running"
    }

    $models = ollama list 2>$null
    if ($models -notlike "*crabdeck*") {
        WARN "Building crabdeck Ollama model (first time)…"
        $mfPath = Join-Path $Root "ollama\Modelfile"
        if (Test-Path $mfPath) {
            ollama create crabdeck -f $mfPath | Out-Null
            OK "crabdeck model created"
        } else {
            WARN "Modelfile not found at $mfPath — skipping model build"
        }
    } else {
        OK "crabdeck Ollama model already exists"
    }
}

# ── 2. Gateway ────────────────────────────────────────────────────────────────
if (-not $UIOnly) {
    STEP "Starting CrabDeck Gateway"
    $gwPath = Join-Path $Root "gateway"
    Import-DotEnv (Join-Path $gwPath ".env")
    if (-not $env:GATEWAY_TOKEN) { WARN "No GATEWAY_TOKEN set — gateway will run in OPEN (no-auth) mode." }
    if (-not (Test-Path "$gwPath\node_modules")) {
        Write-Host "    Installing gateway dependencies…" -ForegroundColor DarkGray
        Push-Location $gwPath; npm install --silent; Pop-Location
    }
    Start-Process powershell -ArgumentList `
        "-NoExit -Command `"Set-Location '$gwPath'; Write-Host 'CrabDeck Gateway' -ForegroundColor Cyan; node server.js`"" `
        -WindowStyle Normal
    Start-Sleep -Milliseconds 800
    OK "Gateway started  → ws://localhost:8765  |  http://localhost:8765/health"
}

# ── 3. Orchestrator Core ──────────────────────────────────────────────────────
$orchPath = Join-Path $Root "orchestrator"
if (Test-Path $orchPath) {
    STEP "Starting Orchestrator Core"
    Import-DotEnv (Join-Path $orchPath ".env")
    $venvPy = "$orchPath\.venv\Scripts\python.exe"
    if (-not (Test-Path $venvPy)) {
        Write-Host "    Creating Python venv…" -ForegroundColor DarkGray
        Push-Location $orchPath
        python -m venv .venv
        & ".venv\Scripts\pip.exe" install -r requirements.txt --quiet
        Pop-Location
        OK "Orchestrator venv ready"
    }
    Start-Process powershell -ArgumentList `
        "-NoExit -Command `"Set-Location '$orchPath'; .venv\Scripts\activate; Write-Host 'Orchestrator Core' -ForegroundColor Magenta; uvicorn main:app --reload --host 0.0.0.0 --port 8000`"" `
        -WindowStyle Normal
    Start-Sleep -Milliseconds 600
    OK "Orchestrator started  → http://localhost:8000  |  /docs"
}

# ── 4. Hermes + OpenClaw agents ───────────────────────────────────────────────
$agentsPath = Join-Path $Root "agents"
if ((Test-Path $agentsPath) -and (-not $UIOnly) -and (-not $GatewayOnly)) {
    STEP "Starting Hermes + OpenClaw agents"
    Import-DotEnv (Join-Path $agentsPath ".env")
    $venvPy = "$agentsPath\.venv\Scripts\python.exe"
    if (-not (Test-Path $venvPy)) {
        Write-Host "    Creating Python venv…" -ForegroundColor DarkGray
        Push-Location $agentsPath
        python -m venv .venv
        & ".venv\Scripts\pip.exe" install -r requirements.txt --quiet
        Pop-Location
    }
    Start-Process powershell -ArgumentList `
        "-NoExit -Command `"Set-Location '$agentsPath'; Write-Host 'Hermes Agent' -ForegroundColor Magenta; .\.venv\Scripts\python hermes_agent.py`"" `
        -WindowStyle Normal
    Start-Process powershell -ArgumentList `
        "-NoExit -Command `"Set-Location '$agentsPath'; Write-Host 'OpenClaw Agent' -ForegroundColor DarkYellow; .\.venv\Scripts\python openclaw_agent.py`"" `
        -WindowStyle Normal
    OK "Hermes + OpenClaw agents started"
}

# ── 5. Vite UI ────────────────────────────────────────────────────────────────
if (-not $GatewayOnly) {
    STEP "Starting CrabDeck UI (Vite)"
    $uiPath = Join-Path $Root "ui"
    if (-not (Test-Path "$uiPath\node_modules")) {
        Write-Host "    Installing UI dependencies (first time, takes ~30s)…" -ForegroundColor DarkGray
        Push-Location $uiPath; npm install --silent; Pop-Location
    }
    Start-Process powershell -ArgumentList `
        "-NoExit -Command `"Set-Location '$uiPath'; Write-Host 'CrabDeck UI' -ForegroundColor Cyan; npm run dev`"" `
        -WindowStyle Normal
    Start-Sleep -Seconds 2
    if (-not $NoBrowser) { Start-Process "http://localhost:5173" }
    OK "UI starting  → http://localhost:5173"
}

# ── Summary ───────────────────────────────────────────────────────────────────
Write-Host @"

  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
   🦀  Team CrabDeck is ONLINE
  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
   UI           http://localhost:5173
   Gateway      ws://localhost:8765
   Orchestrator http://localhost:8000/docs
   Ollama       http://localhost:11434
   Hermes model ollama run crabdeck
  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

"@ -ForegroundColor Cyan
