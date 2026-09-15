@echo off
rem One MT5 executor sync. Task Scheduler runs this every 10 minutes.
rem   run_executor.cmd                   .env,      logs\mt5_executor_demo.log
rem   run_executor.cmd .env.live live    a second executor for a live terminal
cd /d "%~dp0\..\.."
if not exist logs mkdir logs
set "ENVFILE=%~1"
if "%ENVFILE%"=="" set "ENVFILE=.env"
set "LOGTAG=%~2"
if "%LOGTAG%"=="" set "LOGTAG=demo"
".venv-windows\Scripts\python.exe" -m live.mt5_executor --env-file "%ENVFILE%" >> "logs\mt5_executor_%LOGTAG%.log" 2>&1
