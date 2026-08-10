@echo off
REM ticket-master.bat — Windows CMD dispatcher for ticket-master
REM Usage: bin\ticket-master.bat [--provider claude|codex|agy]
setlocal

set "SCRIPT_DIR=%~dp0"
set "REPO_ROOT=%SCRIPT_DIR%.."

set "PROVIDER=claude"
if defined TM_PROVIDER set "PROVIDER=%TM_PROVIDER%"

:parse
if "%~1"=="--provider" (
    set "PROVIDER=%~2"
    shift & shift & goto parse
)
if not "%~1"=="" (
    echo Unknown argument: %~1
    exit /b 1
)

python "%SCRIPT_DIR%ticket_master.py" --provider "%PROVIDER%"
exit /b %ERRORLEVEL%
