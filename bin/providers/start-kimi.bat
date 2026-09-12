@echo off
REM start-kimi.bat - launch ticket-master with Kimi
call "%~dp0..\ticket-master.bat" --provider kimi %*
