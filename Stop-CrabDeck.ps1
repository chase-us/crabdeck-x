# Stop-CrabDeck.ps1 — Kill all Team CrabDeck processes
Write-Host "🦀 Stopping Team CrabDeck..." -ForegroundColor Yellow
Get-Process -Name 'node'    -ErrorAction SilentlyContinue | Stop-Process -Force
Get-Process -Name 'uvicorn' -ErrorAction SilentlyContinue | Stop-Process -Force
Write-Host "  [OK] All CrabDeck processes stopped." -ForegroundColor Green
