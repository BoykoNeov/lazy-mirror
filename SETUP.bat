@echo off
title LazyMirror — Setup
color 0A
echo.
echo  ============================================
echo    LazyMirror — First-time Setup
echo  ============================================
echo.

REM ── Check Python ─────────────────────────────────────────────────────────
python --version >nul 2>&1
if errorlevel 1 (
    echo  [ERROR] Python not found in PATH.
    echo.
    echo  Please install Python 3.10+ from:
    echo    https://python.org/downloads
    echo.
    echo  IMPORTANT: During install, check the box:
    echo    "Add Python to PATH"
    echo.
    pause
    exit /b 1
)

echo  [OK] Python found:
python --version
echo.

REM ── Install pip packages ──────────────────────────────────────────────────
echo  [1/3] Installing Python packages...
echo        (mitmproxy, flask, pystray, Pillow)
echo.
pip install mitmproxy flask pystray Pillow
if errorlevel 1 (
    echo.
    echo  [ERROR] pip install failed.
    echo  Try running this script as Administrator.
    pause
    exit /b 1
)
echo.

REM ── Verify mitmdump is available ──────────────────────────────────────────
where mitmdump >nul 2>&1
if errorlevel 1 (
    echo  [!] mitmdump not found in PATH after install.
    echo      Trying to add Python Scripts to PATH...
    for /f "delims=" %%i in ('python -c "import sys,os; print(os.path.join(os.path.dirname(sys.executable),'Scripts'))"') do set SCRIPTS=%%i
    set PATH=%PATH%;%SCRIPTS%
    where mitmdump >nul 2>&1
    if errorlevel 1 (
        echo  [!] Still not found. You may need to restart your
        echo      command prompt / PC after setup.
    ) else (
        echo  [OK] mitmdump found at: %SCRIPTS%
    )
) else (
    echo  [OK] mitmdump is available
)
echo.

REM ── Generate the CA certificate ───────────────────────────────────────────
echo  [2/3] Generating mitmproxy CA certificate...
echo        (Running proxy for 4 seconds to create cert files)
echo.

if not exist "certs" mkdir certs

REM Run mitmdump briefly in background to trigger cert generation
start /B "" mitmdump --listen-host 127.0.0.1 --listen-port 18099 --set confdir=certs -q
timeout /t 4 /nobreak >nul
taskkill /F /IM mitmdump.exe >nul 2>&1
REM Also kill any python running mitmdump
taskkill /F /IM python.exe /FI "CommandLine eq *mitmdump*" >nul 2>&1

REM Check if cert was created
if exist "certs\mitmproxy-ca-cert.cer" (
    echo  [OK] Certificate created: certs\mitmproxy-ca-cert.cer
) else if exist "certs\mitmproxy-ca-cert.pem" (
    echo  [OK] Certificate created: certs\mitmproxy-ca-cert.pem
    copy "certs\mitmproxy-ca-cert.pem" "certs\mitmproxy-ca-cert.cer" >nul
    echo      Copied as .cer for Windows install
) else (
    echo  [!] Certificate files not generated yet.
    echo      They will be created on first launch of START.bat
    echo      Then run install_cert.bat afterward.
)
echo.

echo  [3/3] Setup complete!
echo.
echo  ============================================
echo   NEXT STEPS:
echo  ============================================
echo.
echo   1. Run: install_cert.bat
echo      (Installs the HTTPS certificate — do once)
echo.
echo   2. Run: configure_proxy.bat  then press [1]
echo      (Routes your browser through the proxy)
echo.
echo   3. Run: START.bat
echo      (Launches LazyMirror + dashboard)
echo.
echo   4. Browse any site in Chrome or Edge
echo      Everything you visit gets cached!
echo.
pause
