@echo off
title LazyMirror — Install Certificate
echo.
echo  ============================================
echo    Install mitmproxy CA Certificate
echo  ============================================
echo.
echo  This installs a local certificate so the proxy
echo  can intercept HTTPS traffic on your machine.
echo.
echo  The cert is generated locally — it never leaves
echo  your PC and is only used by LazyMirror.
echo.

REM ── Generate cert if certs folder is empty ────────────────────────────────
if not exist "certs" mkdir certs

set CERT_CER=certs\mitmproxy-ca-cert.cer
set CERT_PEM=certs\mitmproxy-ca-cert.pem

if not exist "%CERT_CER%" if not exist "%CERT_PEM%" (
    echo  Certificate not found — generating now...
    echo  (Launching proxy briefly to create cert files)
    echo.
    start /B "" mitmdump --listen-host 127.0.0.1 --listen-port 18099 --set confdir=certs -q
    timeout /t 5 /nobreak >nul
    taskkill /F /IM mitmdump.exe >nul 2>&1
    echo  Done.
    echo.
)

REM Copy .pem as .cer if needed (Windows prefers .cer extension)
if not exist "%CERT_CER%" (
    if exist "%CERT_PEM%" (
        copy "%CERT_PEM%" "%CERT_CER%" >nul
        echo  Copied .pem as .cer
    )
)

REM Final check
if not exist "%CERT_CER%" (
    echo  [ERROR] Could not generate certificate.
    echo  Make sure mitmproxy installed correctly (run SETUP.bat first).
    pause
    exit /b 1
)

echo  Certificate file: %CERT_CER%
echo.

REM ── Install into Windows cert store ──────────────────────────────────────
echo  Installing into Windows Trusted Root store...
certutil -addstore -f Root "%CERT_CER%"

if errorlevel 1 (
    echo.
    echo  [!] certutil failed. Opening manual install dialog...
    start "" "%CERT_CER%"
    echo.
    echo  In the Certificate dialog that opens:
    echo    1. Click "Install Certificate"
    echo    2. Choose: "Local Machine"  (or Current User)
    echo    3. Select: "Place all certificates in the following store"
    echo    4. Click Browse → select "Trusted Root Certification Authorities"
    echo    5. Click OK → Next → Finish
    echo.
    echo  Press any key after installing the cert manually.
    pause
) else (
    echo.
    echo  [OK] Certificate installed into Windows Trusted Root store!
    echo.
    echo  Chrome and Edge will now trust the proxy certificate.
)

echo.
echo  ── Firefox users ────────────────────────────────────────────────────
echo  Firefox manages its own cert store. You must also:
echo    1. Open Firefox → Settings → search "Certificates"
echo    2. Click "View Certificates" → "Authorities" tab → "Import"
echo    3. Select: certs\mitmproxy-ca-cert.cer
echo    4. Check "Trust this CA to identify websites" → OK
echo  ─────────────────────────────────────────────────────────────────────
echo.
echo  Setup complete! Now run configure_proxy.bat then START.bat
echo.
pause
