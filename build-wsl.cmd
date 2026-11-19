@echo off
REM Run from CMD: double-click this file, or:
REM   build-wsl.cmd -Distro Ubuntu-24.04 -Tag bettafish_modify:1.1.0
REM Do not use "..." in paths; use the full path to this folder when calling from elsewhere.

setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0build-wsl.ps1" %*
set EXITCODE=%ERRORLEVEL%
endlocal & exit /b %EXITCODE%
