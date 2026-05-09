@echo off
title LazyMirror — Browser Proxy Setup
echo.
echo  ============================================
echo    Configure Browser Proxy
echo  ============================================
echo.
echo  LazyMirror proxy address: 127.0.0.1:8080
echo.
echo  [1] Enable proxy  (route browser through LazyMirror)
echo  [2] Disable proxy (restore direct internet)
echo  [3] Show current proxy settings
echo  [4] Exit
echo.
set /p choice=Enter choice (1/2/3/4): 

if "%choice%"=="1" goto enable
if "%choice%"=="2" goto disable
if "%choice%"=="3" goto show
goto end

:enable
echo.
echo  Enabling Windows system proxy (127.0.0.1:8080)...
reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings" /v ProxyEnable /t REG_DWORD /d 1 /f >nul
reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings" /v ProxyServer /t REG_SZ /d "127.0.0.1:8080" /f >nul
reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings" /v ProxyOverride /t REG_SZ /d "localhost;127.0.0.1;<local>" /f >nul

echo.
echo  [OK] System proxy set to 127.0.0.1:8080
echo.
echo  Chrome and Edge: Will use this automatically (may need restart).
echo.
echo  Firefox: Does NOT use Windows system proxy by default.
echo    → Firefox Settings → General → scroll to Network Settings
echo    → Click "Settings..." → Manual Proxy
echo    → HTTP Proxy: 127.0.0.1   Port: 8080
echo    → Check "Also use this proxy for HTTPS"
echo    → OK
echo.
echo  Now launch LazyMirror with START.bat and start browsing!
goto end

:disable
echo.
echo  Disabling Windows system proxy...
reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings" /v ProxyEnable /t REG_DWORD /d 0 /f >nul
echo  [OK] Direct internet connection restored.
echo.
echo  Remember to also disable proxy in Firefox if you set it there.
goto end

:show
echo.
echo  Current proxy settings:
reg query "HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings" /v ProxyEnable
reg query "HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings" /v ProxyServer
goto end

:end
echo.
pause
