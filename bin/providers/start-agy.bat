@echo off
REM start-agy.bat - launch ticket-master with agy (Gemini)
call "%~dp0..\ticket-master.bat" --provider agy %*
