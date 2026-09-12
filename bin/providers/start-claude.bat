@echo off
REM start-claude.bat - launch ticket-master with Claude
call "%~dp0..\ticket-master.bat" --provider claude %*
