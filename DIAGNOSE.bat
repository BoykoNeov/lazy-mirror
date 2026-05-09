@echo off
title LazyMirror — Diagnostics
echo.
echo  ============================================
echo    LazyMirror Diagnostics
echo  ============================================
echo.

echo  [1] Python version:
python --version 2>&1
echo.

echo  [2] pip packages:
pip show mitmproxy flask pystray 2>&1 | findstr "Name Version"
echo.

echo  [3] mitmdump location:
where mitmdump 2>&1
echo.

echo  [4] Python Scripts folder:
python -c "import sys,os; print(os.path.join(os.path.dirname(sys.executable),'Scripts'))" 2>&1
echo.

echo  [5] mitmdump in Scripts folder:
python -c "import sys,os; p=os.path.join(os.path.dirname(sys.executable),'Scripts','mitmdump.exe'); print('EXISTS' if os.path.exists(p) else 'NOT FOUND', p)" 2>&1
echo.

echo  [6] Cert files:
dir certs 2>&1
echo.

echo  [7] Cache folder:
dir offline_cache 2>&1
echo.

echo  [8] Proxy currently listening on 8080?
netstat -ano | findstr ":8080"
echo.

echo  [9] Port 7779 (dashboard)?
netstat -ano | findstr ":7779"
echo.

echo  [10] Proxy log (last 20 lines):
if exist "logs\proxy.log" (
    powershell -Command "Get-Content 'logs\proxy.log' -Tail 20"
) else (
    echo  (no log file yet)
)
echo.

echo  ============================================
echo  Copy the output above when reporting issues.
echo  ============================================
echo.
pause
