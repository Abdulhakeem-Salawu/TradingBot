@echo off
rem Every paper-trading job once (hourly from Task Scheduler). Pass --compare for the daily report.
cd /d "%~dp0\..\.."
if not exist logs mkdir logs
".venv-windows\Scripts\python.exe" -m live.run_jobs %* >> "logs\run_jobs.log" 2>&1
