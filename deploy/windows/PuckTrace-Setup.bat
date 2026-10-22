@echo off
rem PuckTrace installer for the office PC: right-click > Run as administrator.
rem It downloads the real installer (setup.ps1) from GitHub, so it can be fixed without a new download.
title PuckTrace setup
net session >nul 2>&1
if errorlevel 1 (
  echo Asking for administrator rights...
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)
set "PS1=%TEMP%\pucktrace-setup.ps1"
powershell -NoProfile -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; try { Invoke-WebRequest -UseBasicParsing 'https://raw.githubusercontent.com/Ajinkyaa2004/Hudl/main/deploy/windows/setup.ps1' -OutFile '%PS1%' } catch { Write-Host 'Could not download the installer:' $_.Exception.Message -ForegroundColor Red; exit 1 }"
if errorlevel 1 (
  echo.
  echo The installer could not be downloaded. Check that github.com opens in Chrome on this PC.
  pause
  exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%"
echo.
pause
