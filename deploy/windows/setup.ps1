# PuckTrace office-PC installer. Started by PuckTrace-Setup.bat (as administrator).
# Installs Python, PuckTrace (a Windows task that starts at boot), the update checker and the
# Cloudflare tunnel, in that order, and checks each step. Everything is logged to C:\PuckTrace\logs\setup.log.
$ErrorActionPreference = 'Stop'
$Root = 'C:\PuckTrace'
$Repo = 'Ajinkyaa2004/Hudl'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
try { [System.Net.WebRequest]::DefaultWebProxy.Credentials = [System.Net.CredentialCache]::DefaultNetworkCredentials } catch {}
New-Item -ItemType Directory -Force -Path "$Root\logs", "$Root\tmp" | Out-Null
Start-Transcript -Path "$Root\logs\setup.log" -Append | Out-Null

$summary = [System.Collections.Generic.List[string]]::new()
function Step($t)  { Write-Host ""; Write-Host "== $t" -ForegroundColor Cyan }
function Ok($t)    { Write-Host "   OK   $t" -ForegroundColor Green; $script:summary.Add("OK    $t") }
function Warn($t)  { Write-Host "   WARN $t" -ForegroundColor Yellow; $script:summary.Add("WARN  $t") }
function Fail($t)  { Write-Host "   FAIL $t" -ForegroundColor Red; $script:summary.Add("FAIL  $t") }
function Download($url, $out) { Invoke-WebRequest -UseBasicParsing -TimeoutSec 600 $url -OutFile $out }
function Health { try { Invoke-RestMethod -UseBasicParsing -TimeoutSec 8 'http://127.0.0.1:8000/api/healthz' } catch { $null } }

try {
    Write-Host "PuckTrace setup for this PC" -ForegroundColor White
    Write-Host "Log: $Root\logs\setup.log"

    # ---------------------------------------------------------------- 1. the three secrets
    Step "1/8  Reading the secrets file"
    $secrets = @{}
    $files = @()
    foreach ($d in (Get-ChildItem 'C:\Users' -Directory -ErrorAction SilentlyContinue)) {
        $files += Get-ChildItem "$($d.FullName)\Downloads" -Filter 'PuckTrace-secrets*.txt' -ErrorAction SilentlyContinue
    }
    $files += Get-ChildItem $Root -Filter 'PuckTrace-secrets*.txt' -ErrorAction SilentlyContinue
    $file = $files | Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if ($file) {
        foreach ($line in Get-Content $file.FullName) {
            if ($line -match '^\s*([A-Z_]+)\s*=\s*(.+?)\s*$') { $secrets[$Matches[1]] = $Matches[2] }
        }
        Ok "read $($file.FullName)"
    }
    foreach ($k in 'APP_PASSWORD', 'APP_SECRET', 'TUNNEL_TOKEN') {
        if (-not $secrets[$k]) { $secrets[$k] = Read-Host "Type $k (no secrets file found)" }
        if (-not $secrets[$k]) { throw "$k is missing" }
    }

    # ---------------------------------------------------------------- 2. can this PC reach everything?
    Step "2/8  Checking the network"
    foreach ($h in 'github.com', 'pypi.org', 'files.pythonhosted.org', 'www.python.org') {
        $t = Test-NetConnection $h -Port 443 -WarningAction SilentlyContinue
        if ($t.TcpTestSucceeded) { Ok "$h reachable" } else { Fail "$h is NOT reachable (port 443)" }
    }
    $tunnelOk = $false
    foreach ($h in 'region1.v2.argotunnel.com', 'region2.v2.argotunnel.com') {
        $t = Test-NetConnection $h -Port 7844 -WarningAction SilentlyContinue
        if ($t.TcpTestSucceeded) { Ok "$h port 7844 reachable (Cloudflare tunnel)"; $tunnelOk = $true } else { Warn "$h port 7844 is NOT reachable" }
    }
    if (-not $tunnelOk) { Warn "The Cloudflare tunnel needs outgoing port 7844. Ask IT to allow it; the server will still be installed." }

    # ---------------------------------------------------------------- 3. never sleep
    Step "3/8  Power settings (never sleep)"
    powercfg /change standby-timeout-ac 0 | Out-Null
    powercfg /change hibernate-timeout-ac 0 | Out-Null
    powercfg /change disk-timeout-ac 0 | Out-Null
    powercfg /hibernate off | Out-Null
    Ok "sleep and hibernate off while plugged in (the screen may still turn off)"

    # ---------------------------------------------------------------- 4. Python
    Step "4/8  Python 3.11"
    $py = $null
    foreach ($c in 'C:\Program Files\Python311\python.exe', 'C:\Python311\python.exe') { if (Test-Path $c) { $py = $c; break } }
    if (-not $py) {
        Write-Host "   downloading and installing Python (about 1 minute)..."
        Download 'https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe' "$Root\tmp\python-installer.exe"
        $p = Start-Process "$Root\tmp\python-installer.exe" -ArgumentList '/quiet', 'InstallAllUsers=1', 'PrependPath=0', 'Include_test=0', 'Include_launcher=0' -Wait -PassThru
        if ($p.ExitCode -ne 0) { throw "Python installer exit code $($p.ExitCode)" }
        $py = 'C:\Program Files\Python311\python.exe'
    }
    if (-not (Test-Path $py)) { throw "Python not found after install" }
    Ok (& $py --version)

    # ---------------------------------------------------------------- 5. PuckTrace code and packages
    Step "5/8  PuckTrace code and packages"
    $c = (Invoke-WebRequest -UseBasicParsing -TimeoutSec 30 "https://api.github.com/repos/$Repo/commits/main" -Headers @{ Accept = 'application/vnd.github.sha'; 'User-Agent' = 'pucktrace-setup' }).Content
    if ($c -is [byte[]]) { $c = [System.Text.Encoding]::UTF8.GetString($c) }
    $sha = ([string]$c).Trim()
    $short = $sha.Substring(0, 7)
    Download "https://github.com/$Repo/archive/$sha.zip" "$Root\tmp\src.zip"
    Remove-Item "$Root\tmp\x" -Recurse -Force -ErrorAction SilentlyContinue
    Expand-Archive "$Root\tmp\src.zip" -DestinationPath "$Root\tmp\x" -Force
    $src = (Get-ChildItem "$Root\tmp\x" -Directory | Select-Object -First 1).FullName
    New-Item -ItemType Directory -Force -Path "$Root\app" | Out-Null
    robocopy "$src\backend"  "$Root\app\backend"  /MIR /NFL /NDL /NJH /NJS /NP | Out-Null
    robocopy "$src\frontend" "$Root\app\frontend" /MIR /NFL /NDL /NJH /NJS /NP | Out-Null
    robocopy "$src\data"     "$Root\app\data"     /E   /NFL /NDL /NJH /NJS /NP | Out-Null
    robocopy "$src\deploy\windows" "$Root\deploy" /E /NFL /NDL /NJH /NJS /NP | Out-Null
    Copy-Item "$src\requirements.txt" "$Root\app\requirements.txt" -Force
    Set-Content "$Root\version.txt" $short
    Copy-Item "$Root\deploy\run.cmd" "$Root\run.cmd" -Force
    Copy-Item "$Root\deploy\update.ps1" "$Root\update.ps1" -Force
    Ok "code version $short copied to $Root\app"
    if (-not (Test-Path "$Root\venv\Scripts\python.exe")) { & $py -m venv "$Root\venv" }
    Write-Host "   installing packages (a few minutes)..."
    & "$Root\venv\Scripts\python.exe" -m pip install -q --disable-pip-version-check -r "$Root\app\requirements.txt" tzdata
    if ($LASTEXITCODE -ne 0) { throw "pip install failed" }
    Ok "packages installed"

    # secrets for the server (readable by administrators and the system only)
    Set-Content "$Root\env.cmd" "@echo off`r`nset APP_PASSWORD=$($secrets['APP_PASSWORD'])`r`nset APP_SECRET=$($secrets['APP_SECRET'])`r`nset PORT=8000"
    icacls "$Root\env.cmd" /inheritance:r /grant:r 'SYSTEM:(F)' 'BUILTIN\Administrators:(F)' | Out-Null

    # ---------------------------------------------------------------- 6. the server task and the update task
    Step "6/8  Start-at-boot tasks"
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
        -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 99 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
    $principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
    Unregister-ScheduledTask -TaskName 'PuckTrace' -Confirm:$false -ErrorAction SilentlyContinue
    Register-ScheduledTask -TaskName 'PuckTrace' -Principal $principal -Settings $settings `
        -Action (New-ScheduledTaskAction -Execute 'cmd.exe' -Argument '/c C:\PuckTrace\run.cmd') -Trigger (New-ScheduledTaskTrigger -AtStartup) | Out-Null
    Unregister-ScheduledTask -TaskName 'PuckTrace Update' -Confirm:$false -ErrorAction SilentlyContinue
    $upd = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 2)
    $updSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 20) -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName 'PuckTrace Update' -Principal $principal -Settings $updSettings `
        -Action (New-ScheduledTaskAction -Execute 'powershell.exe' -Argument '-NoProfile -ExecutionPolicy Bypass -File C:\PuckTrace\update.ps1') -Trigger $upd | Out-Null
    Ok "tasks 'PuckTrace' (at boot) and 'PuckTrace Update' (every 2 minutes) created"
    Start-ScheduledTask -TaskName 'PuckTrace'

    # ---------------------------------------------------------------- 7. wait for the server to be complete
    Step "7/8  Waiting for the server to load its knowledge and game list (up to 8 minutes)"
    $h = $null
    for ($i = 0; $i -lt 96; $i++) {
        Start-Sleep -Seconds 5
        $h = Health
        if ($h -and [int]$h.kb_teams -gt 5000 -and [int]$h.game_list -gt 100000) { break }
        if ($i % 6 -eq 5) { Write-Host ("   ... " + $(if ($h) { "answering; knowledge teams $($h.kb_teams), games $($h.game_list)" } else { "starting" })) }
    }
    if ($h -and [int]$h.kb_teams -gt 5000) { Ok "server answers: version $($h.version), $($h.kb_teams) teams, $($h.game_list) games" }
    elseif ($h) { Fail "server answers but has no knowledge yet (teams $($h.kb_teams), games $($h.game_list)); see $Root\logs\server.log" }
    else { Fail "server does not answer; see $Root\logs\server.log" }

    # ---------------------------------------------------------------- 8. the tunnel (last: only once the server is complete)
    Step "8/8  Cloudflare tunnel"
    if ($h -and [int]$h.kb_teams -gt 5000) {
        $cf = 'C:\Program Files (x86)\cloudflared\cloudflared.exe'
        if (-not (Test-Path $cf)) {
            Download 'https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.msi' "$Root\tmp\cloudflared.msi"
            $p = Start-Process msiexec.exe -ArgumentList '/i', "$Root\tmp\cloudflared.msi", '/qn' -Wait -PassThru
            if ($p.ExitCode -ne 0) { throw "cloudflared installer exit code $($p.ExitCode)" }
        }
        if (-not (Test-Path $cf)) { throw "cloudflared not found after install" }
        if (Get-Service Cloudflared -ErrorAction SilentlyContinue) { & $cf service uninstall | Out-Null; Start-Sleep -Seconds 3 }
        & $cf service install $secrets['TUNNEL_TOKEN'] | Out-Null
        Start-Sleep -Seconds 8
        $svc = Get-Service Cloudflared -ErrorAction SilentlyContinue
        if ($svc -and $svc.Status -eq 'Running') { Ok "tunnel service running (nas.actorix.in now also reaches this PC)" }
        else { Fail "tunnel service is not running (likely port 7844 blocked)" }
    } else {
        Warn "tunnel not installed: the server is not complete yet"
    }

    # the secrets file is no longer needed
    foreach ($f in $files) { Remove-Item $f.FullName -Force -ErrorAction SilentlyContinue }
    if ($files) { Ok "secrets file deleted from this PC (delete it from Google Drive too)" }
}
catch {
    Fail $_.Exception.Message
}

Write-Host ""
Write-Host "================ SUMMARY (take a photo of this screen) ================" -ForegroundColor White
$summary | ForEach-Object {
    $c = if ($_ -like 'OK*') { 'Green' } elseif ($_ -like 'WARN*') { 'Yellow' } else { 'Red' }
    Write-Host $_ -ForegroundColor $c
}
Write-Host "Computer: $env:COMPUTERNAME   Time: $(Get-Date -Format 'yyyy-MM-dd HH:mm')"
Write-Host "======================================================================="
Stop-Transcript | Out-Null
