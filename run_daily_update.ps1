# 警告：server.py 內建的 daily_update_worker 背景執行緒已經會在每天 08:30
# 後自動判斷「今天有沒有成功過」並呼叫 daily_update.py 的 run()，不需要
# 再另外用 Windows工作排程器把這支 .ps1 註冊成每日排程——兩條路徑各自
# 獨立判斷「今天跑過沒」、各自啟動 full_daily_update()，只靠 server.py
# 行程內的 threading.Lock 互斥，那個鎖完全無法跨行程生效，唯一的跨行程
# 保護只剩 SQLite busy_timeout + 有限次數重試，兩條路徑同時搶著抓價/寫
# 資料庫會大幅提高 "database is locked" 的機率(這正是本系統已知踩過的
# 真實故障模式之一)。這支腳本只保留給「想要手動、或用不同排程機制單獨
# 觸發一次每日更新」的情境使用，不要跟 server.py 常駐執行時的自動排程
# 重疊註冊。
$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$LogDir = Join-Path $ProjectRoot "daily_update_logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
Set-Location $ProjectRoot
python .\daily_update.py 1>> (Join-Path $LogDir "scheduled_task_stdout.log") 2>> (Join-Path $LogDir "scheduled_task_stderr.log")
exit $LASTEXITCODE
