# Manage-LLM.ps1 — Keeps Ollama alive (watchdog loop)
# Run in background: Start-Process powershell -ArgumentList "-File Manage-LLM.ps1" -WindowStyle Minimized

Write-Host "Ollama Watchdog running..." -ForegroundColor Cyan
while ($true) {
    $svc = Get-Process "ollama" -ErrorAction SilentlyContinue
    if (-not $svc) {
        Write-Host "$(Get-Date -f 'HH:mm:ss') Ollama down — restarting..." -ForegroundColor Red
        Start-Process "ollama" -ArgumentList "serve" -WindowStyle Minimized
        Start-Sleep -Seconds 5
    }
    Start-Sleep -Seconds 30
}
