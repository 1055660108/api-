@echo off
setlocal

cd /d "%~dp0"

set "APP_URL=http://127.0.0.1:8088/admin"
set "PLAYWRIGHT_BROWSERS_PATH=%CD%\.pw-browsers"
set "PYTHON_EXE=%CD%\.venv\Scripts\python.exe"

if not exist "%PYTHON_EXE%" (
  echo Python venv not found: %PYTHON_EXE%
  echo Please install dependencies first.
  pause
  exit /b 1
)

echo Starting Dola Fetch Service...
echo Admin URL: %APP_URL%
echo.

start "Dola Worker" /min "%PYTHON_EXE%" worker.py
start "" "%APP_URL%"
"%PYTHON_EXE%" run.py

pause
