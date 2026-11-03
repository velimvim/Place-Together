@echo off
setlocal enabledelayedexpansion
title PlaceTogether Dedicated Server Launcher

rem ============================================================
rem  PlaceTogether 3.1 - Fast Dedicated Server Launcher
rem  - Instant startup (no slow PowerShell WMI queries)
rem  - Native Python network IP detection
rem  - Auto-restarts on crash, pauses on error
rem ============================================================

set "PORT=8787"

rem --- Server file: ONLY next to this bat ---------------------
set "SRV=%~dp0server.py"

if not exist "%SRV%" (
    echo.
    echo   ====================================================
    echo    [ERROR] server.py not found
    echo   ====================================================
    echo.
    echo   Expected file:
    echo     %SRV%
    echo.
    echo   Put server.py into the SAME folder as this .bat:
    echo     %~dp0
    echo.
    pause
    exit /b 1
)

rem --- Find Python: py launcher, standard python, embed -------
set "PY_CMD="

py -3 --version >nul 2>&1
if not errorlevel 1 set "PY_CMD=py -3"

if not defined PY_CMD (
    python --version >nul 2>&1
    if not errorlevel 1 set "PY_CMD=python"
)

if not defined PY_CMD if exist "%~dp0python_embed\python.exe" (
    set "PY_CMD=%~dp0python_embed\python.exe"
)

if not defined PY_CMD (
    echo.
    echo   ====================================================
    echo    [ERROR] Python not found
    echo   ====================================================
    echo.
    echo   Install Python 3.10+ from https://www.python.org/downloads/
    echo   and tick "Add python.exe to PATH" during installation.
    echo.
    pause
    exit /b 1
)

rem --- Database file next to server.py ------------------------
for %%i in ("%SRV%") do set "SRVDIR=%%~dpi"
set "DB_FILE=%SRVDIR%placetogether.db"

echo.
echo   ====================================================
echo     PlaceTogether 3.2 - Launching Dedicated Server...
echo   ====================================================
echo   Python    : !PY_CMD!
echo   Server    : %SRV%
echo   Database  : %DB_FILE%
echo   Port      : %PORT%
echo.

rem --- Run server directly (Instant < 0.2s startup) -----------
!PY_CMD! "%SRV%" --port %PORT% --db "%DB_FILE%" %*
set "EXITCODE=%errorlevel%"

echo.
echo   ====================================================
echo     Server stopped. Exit code: %EXITCODE%
echo   ====================================================
echo.
if not "%EXITCODE%"=="0" (
    echo   The server exited with an error.
    echo   Scroll up to read the traceback above.
    echo.
)
pause
endlocal
