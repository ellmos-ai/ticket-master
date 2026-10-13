@echo off
chcp 65001 >nul
setlocal
set "PYTHONIOENCODING=utf-8"
REM Optional: TM_LANG=de|en, TASKPLAN_WORKDIR, TASKPLAN_TRUSTED_AUTOMATION.
REM No account, machine, model or permissions are selected by this file.
python -c "import taskplan" >nul 2>&1
if errorlevel 1 (
  echo [FEHLER] TASKPLAN fehlt. Installation: python -m pip install taskplan
  exit /b 2
)
if not defined TM_LANG set "TM_LANG=de"
set "TM_PROMPT=%~dp0..\..\prompts\TICKET-MASTER.%TM_LANG%.md"
if not exist "%TM_PROMPT%" (
  echo [FEHLER] Rollen-Prompt fehlt. TM_LANG muss de oder en sein.
  exit /b 2
)
python -m taskplan launch --label ticket-master --prompt-file "%TM_PROMPT%" --request "Start as TICKET-MASTER. Complete boot steps (a) through (d) and enter POSITION 0." --interactive %*
exit /b %ERRORLEVEL%
