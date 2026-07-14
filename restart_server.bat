@echo off
rem 乾淨重啟台股 server(關掉舊的+起新的),用來套用 server.py 改動或收拾雙開。
rem 盤中不要跑,收盤後(13:30 之後)再雙擊我。
setlocal
pushd "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0restart_server.ps1"
popd
pause
