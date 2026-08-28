# Restart allseer: kill every running instance, start a fresh detached one on :8077.
#   powershell -ExecutionPolicy Bypass -File .\restart.ps1
# Logs land in allseer.log / allseer.err.log (both gitignored via *.log).
# Note: port 8080 is SearXNG in Docker - this script never touches it.

$out = "$PSScriptRoot\allseer.log"
$err = "$PSScriptRoot\allseer.err.log"

# Match on the command line, not the image name - many unrelated python.exe are running.
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -match 'run\.py|allseer' } |
    ForEach-Object {
        "killing PID $($_.ProcessId)"
        Stop-Process -Id $_.ProcessId -Force
    }
Start-Sleep -Seconds 2

# -u so a crash flushes its traceback instead of dying with an empty log.
$p = Start-Process python -ArgumentList '-u', 'run.py' `
    -WorkingDirectory $PSScriptRoot `
    -RedirectStandardOutput $out -RedirectStandardError $err `
    -WindowStyle Hidden -PassThru
"started PID $($p.Id)"

Start-Sleep -Seconds 6
try {
    $code = (Invoke-WebRequest 'http://127.0.0.1:8077/' -UseBasicParsing -TimeoutSec 10).StatusCode
    "allseer up (HTTP $code) -> http://127.0.0.1:8077"
} catch {
    "FAILED to come up. Last lines of $err :"
    Get-Content $err -Tail 20 -ErrorAction SilentlyContinue
    exit 1
}
