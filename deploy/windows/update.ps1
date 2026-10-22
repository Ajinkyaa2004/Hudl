# PuckTrace update checker: a scheduled task runs this every 2 minutes. When a newer version is on
# GitHub it waits until no search is running, replaces the code, restarts the server (run.cmd starts
# it again by itself) and checks that it answers; if it doesn't, the previous code is put back.
$ErrorActionPreference = 'Stop'
$Root = 'C:\PuckTrace'
$Repo = 'Ajinkyaa2004/Hudl'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
try { [System.Net.WebRequest]::DefaultWebProxy.Credentials = [System.Net.CredentialCache]::DefaultNetworkCredentials } catch {}
function Log($m) { Add-Content "$Root\logs\update.log" ("[{0}] {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m) }
function Health { try { Invoke-RestMethod -UseBasicParsing -TimeoutSec 8 'http://127.0.0.1:8000/api/healthz' } catch { $null } }
function StopServer {
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -like '*backend.app*' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}

try {
    $current = (Get-Content "$Root\version.txt" -ErrorAction SilentlyContinue | Select-Object -First 1)
    $etagFile = "$Root\etag.txt"
    $headers = @{ Accept = 'application/vnd.github.sha'; 'User-Agent' = 'pucktrace-updater' }
    if ((Test-Path $etagFile) -and $current) { $headers['If-None-Match'] = (Get-Content $etagFile -Raw).Trim() }
    try {
        $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 20 "https://api.github.com/repos/$Repo/commits/main" -Headers $headers
    } catch {
        if ($_.Exception.Response -and [int]$_.Exception.Response.StatusCode -eq 304) { exit 0 }
        Log "GitHub check failed: $($_.Exception.Message)"; exit 0
    }
    $c = $r.Content
    if ($c -is [byte[]]) { $c = [System.Text.Encoding]::UTF8.GetString($c) }   # Windows PowerShell gives raw bytes for this content type
    $sha = ([string]$c).Trim()
    if ($sha.Length -lt 40) { Log "unexpected answer from GitHub"; exit 0 }
    $short = $sha.Substring(0, 7)
    if ($r.Headers['ETag']) { Set-Content $etagFile ([string]$r.Headers['ETag']) }
    if ($short -eq $current) { exit 0 }
    if ((Test-Path "$Root\bad_version.txt") -and ((Get-Content "$Root\bad_version.txt" -Raw).Trim() -eq $short)) { exit 0 }   # failed to start before: wait for a newer one

    Log "new version $short (running $current)"
    # wait (up to 4 minutes) until nobody is mid-search
    for ($i = 0; $i -lt 24; $i++) {
        $h = Health
        if (-not $h -or [int]$h.searches -eq 0) { break }
        Start-Sleep -Seconds 10
    }

    $tmp = "$Root\tmp"
    Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Path $tmp | Out-Null
    Invoke-WebRequest -UseBasicParsing -TimeoutSec 300 "https://github.com/$Repo/archive/$sha.zip" -OutFile "$tmp\src.zip"
    Expand-Archive "$tmp\src.zip" -DestinationPath "$tmp\x" -Force
    $src = (Get-ChildItem "$tmp\x" -Directory | Select-Object -First 1).FullName

    # keep the previous code for a rollback
    robocopy "$Root\app\backend"  "$Root\prev\backend"  /MIR /NFL /NDL /NJH /NJS /NP | Out-Null
    robocopy "$Root\app\frontend" "$Root\prev\frontend" /MIR /NFL /NDL /NJH /NJS /NP | Out-Null
    Copy-Item "$Root\app\requirements.txt" "$Root\prev\requirements.txt" -Force -ErrorAction SilentlyContinue
    $oldReq = (Get-FileHash "$Root\app\requirements.txt").Hash

    robocopy "$src\backend"  "$Root\app\backend"  /MIR /NFL /NDL /NJH /NJS /NP | Out-Null
    robocopy "$src\frontend" "$Root\app\frontend" /MIR /NFL /NDL /NJH /NJS /NP | Out-Null
    robocopy "$src\data"     "$Root\app\data"     /E   /NFL /NDL /NJH /NJS /NP | Out-Null   # public config files only; private data stays
    robocopy "$src\deploy\windows" "$Root\deploy" /E /NFL /NDL /NJH /NJS /NP | Out-Null
    Copy-Item "$src\requirements.txt" "$Root\app\requirements.txt" -Force
    if ((Get-FileHash "$Root\app\requirements.txt").Hash -ne $oldReq) {
        Log "requirements changed: installing"
        & "$Root\venv\Scripts\python.exe" -m pip install -q -r "$Root\app\requirements.txt" tzdata
    }
    Copy-Item "$Root\deploy\run.cmd" "$Root\run.cmd" -Force
    Copy-Item "$Root\deploy\update.ps1" "$Root\update.ps1.new" -Force   # takes effect on the next run
    Set-Content "$Root\version.txt" $short

    StopServer          # run.cmd starts it again by itself
    $ok = $false
    for ($i = 0; $i -lt 18; $i++) {
        Start-Sleep -Seconds 5
        $h = Health
        if ($h -and $h.version -eq $short) { $ok = $true; break }
    }
    if ($ok) {
        Log "now running $short"
    } else {
        Log "version $short did not start: putting $current back"
        robocopy "$Root\prev\backend"  "$Root\app\backend"  /MIR /NFL /NDL /NJH /NJS /NP | Out-Null
        robocopy "$Root\prev\frontend" "$Root\app\frontend" /MIR /NFL /NDL /NJH /NJS /NP | Out-Null
        Copy-Item "$Root\prev\requirements.txt" "$Root\app\requirements.txt" -Force
        if ($current) { Set-Content "$Root\version.txt" $current }
        Remove-Item $etagFile -Force -ErrorAction SilentlyContinue
        StopServer
        Log "rolled back; will try again on the next new version"
        Set-Content "$Root\bad_version.txt" $short
    }
    Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
    if ($ok) { Remove-Item "$Root\bad_version.txt" -Force -ErrorAction SilentlyContinue }
    if (Test-Path "$Root\update.ps1.new") { try { Move-Item "$Root\update.ps1.new" "$Root\update.ps1" -Force } catch {} }
} catch {
    Log "update failed: $($_.Exception.Message)"
}
