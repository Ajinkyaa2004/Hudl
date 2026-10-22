@echo off
rem PuckTrace server loop: started at boot by the "PuckTrace" scheduled task; if the server stops
rem for any reason (a crash, or the updater ending it) it starts again after 5 seconds.
cd /d C:\PuckTrace\app
call C:\PuckTrace\env.cmd
set LIGHT_MODE=1
set MAX_SEARCHES=8
set GAME_LIST=1
set HEALTH_DAILY=0
set PYTHONUNBUFFERED=1
set PYTHONDONTWRITEBYTECODE=1
:loop
set /p APP_VERSION=<C:\PuckTrace\version.txt
for %%A in (C:\PuckTrace\logs\server.log) do if %%~zA GTR 30000000 move /y C:\PuckTrace\logs\server.log C:\PuckTrace\logs\server.old.log >nul
echo [%date% %time%] starting version %APP_VERSION% >> C:\PuckTrace\logs\server.log
C:\PuckTrace\venv\Scripts\python.exe -m uvicorn backend.app:app --host 127.0.0.1 --port 8000 --timeout-keep-alive 30 >> C:\PuckTrace\logs\server.log 2>&1
echo [%date% %time%] stopped, starting again in 5 s >> C:\PuckTrace\logs\server.log
ping -n 6 127.0.0.1 >nul
goto loop
