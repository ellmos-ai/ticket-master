@echo off
rem Usage: session-results.bat absolute-session-file.json [explicit owner options]
if "%~1"=="" (
  echo Usage: session-results.bat absolute-session-file.json
  exit /b 2
)
ticket-master-results open %*
