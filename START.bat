@echo off
title LazyMirror
cd /d "%~dp0"

REM Add Python Scripts to PATH so mitmdump is found
for /f "delims=" %%i in ('python -c "import sys,os; print(os.path.join(os.path.dirname(sys.executable),'Scripts'))" 2^>nul') do set PYSCRIPTS=%%i
if defined PYSCRIPTS set PATH=%PATH%;%PYSCRIPTS%

python lazymirror.py
echo.
echo LazyMirror has stopped.
pause
