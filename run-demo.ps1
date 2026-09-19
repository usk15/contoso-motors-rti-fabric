param(
    [ValidateSet("normal", "anomaly")]
    [string]$Mode = "anomaly"
)

$ErrorActionPreference = "Stop"

python "$PSScriptRoot\preflight.py"
if ($LASTEXITCODE -ne 0) { throw "Preflight failed. Fix the issues above before running the demo." }

# Credentials are read live from the Fabric REST API by simulator.py.
# Clear any stale manual overrides so they cannot shadow the current endpoint.
Remove-Item Env:\EVENTSTREAM_CONN_STR -ErrorAction SilentlyContinue
Remove-Item Env:\EVENTSTREAM_EH_NAME -ErrorAction SilentlyContinue

if ($Mode -eq "normal") {
    python "$PSScriptRoot\simulator.py" --duration 120
    Start-Sleep -Seconds 30
    python "$PSScriptRoot\validate.py" --require-fresh
}
else {
    python "$PSScriptRoot\simulator.py" --duration 420 --anomaly-machine M-007 --anomaly-window 360
    Start-Sleep -Seconds 30
    python "$PSScriptRoot\validate.py" --require-fresh --require-anomaly
    Write-Host ""
    Write-Host "Now check the MAINTENANCE_EMAIL_TO inbox for the Activator alert (allow 2-5 minutes)." -ForegroundColor Cyan
}
