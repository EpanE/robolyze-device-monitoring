@echo off
REM Dependency install failures are warned about, not fatal: this gets double-clicked in a
REM truck that may have no internet, and a working .venv must still start.
cd /d "%~dp0"

set "PY=.venv\Scripts\python.exe"

if not exist "%PY%" (
    echo Creating virtual environment...
    python -m venv .venv
    if errorlevel 1 (
        echo ERROR: could not create .venv - is python on PATH?
        pause
        exit /b 1
    )
)

REM Skip the install entirely when the pinned set already imports, so a normal start
REM needs no network.
"%PY%" -c "import flask, paramiko, requests, waitress" 2>nul
if errorlevel 1 (
    echo Installing dependencies...
    "%PY%" -m pip install -q -r requirements.txt
    if errorlevel 1 (
        echo WARNING: dependency install failed ^(offline?^). Starting anyway.
    )
)

REM Read PORT from the same .env the app reads rather than assuming 5000.
set "PORT=5000"
for /f "usebackq tokens=1,* delims==" %%A in ("..\.env") do (
    if /i "%%~A"=="PORT" set "PORT=%%~B"
)
set "PORT=%PORT:"=%"
set "PORT=%PORT:'=%"

start "Duromac Dashboard" "%PY%" app.py

timeout /t 2 /nobreak >nul
start "" "http://127.0.0.1:%PORT%/"
