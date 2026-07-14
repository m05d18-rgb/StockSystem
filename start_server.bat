@echo off
setlocal
rem 目前這台機器沒有任何排程/開機自動執行項目會呼叫這個檔案(已用
rem Get-ScheduledTask/Startup資料夾/登錄檔Run機碼確認過)，唯一真正在跑的
rem 啟動路徑是桌面捷徑 open_stock_system.bat -> launch_stock_system.ps1。
rem 如果之後真的要接上工作排程器，務必讓下面這段邏輯也用同一個具名
rem Mutex("StockAI_Server_Startup_Lock")序列化，否則會跟桌面捷徑那條路徑
rem 各自判斷「port還沒監聽」而重複啟動 server.py。

set "PYTHON_EXE=C:\Users\m05d1\AppData\Local\Python\pythoncore-3.14-64\python.exe"
set "ROOT=%~dp0"
set "LOG=%ROOT%server.log"

if not exist "%PYTHON_EXE%" (
  echo %date% %time% [ERROR] Fixed Python not found: %PYTHON_EXE% >> "%LOG%"
  exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -Command "$conn = Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort 8008 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1; if (-not $conn) { exit 1 }; $p = Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue; if ($p -and $p.Path -eq '%PYTHON_EXE%') { exit 0 }; if ($p) { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue; Start-Sleep -Seconds 2 }; exit 1"
if not errorlevel 1 exit /b 0

cd /d "%ROOT%" || exit /b 1

"%PYTHON_EXE%" health_check.py --no-predict >nul 2>nul
if errorlevel 1 (
  echo %date% %time% [StockAI] Installing requirements... >> "%LOG%"
  "%PYTHON_EXE%" -m pip install -r requirements-model.txt >> "%LOG%" 2>&1
)

echo %date% %time% [StockAI] Starting... >> "%LOG%"
"%PYTHON_EXE%" health_check.py --no-predict >> "%LOG%" 2>&1
"%PYTHON_EXE%" server.py >> "%LOG%" 2>&1
