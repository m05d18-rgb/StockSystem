$TaskName = "StockAI_AutoStart"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$VbsFile = Join-Path $Root "run_server_hidden.vbs"

# 移除舊的排程（如果有）
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue

$Action = New-ScheduledTaskAction `
  -Execute "wscript.exe" `
  -Argument "`"$VbsFile`""

$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

$Settings = New-ScheduledTaskSettingsSet `
  -ExecutionTimeLimit (New-TimeSpan -Hours 0) `
  -MultipleInstances IgnoreNew `
  -StartWhenAvailable `
  -DisallowDemandStart:$false

$Principal = New-ScheduledTaskPrincipal `
  -UserId $env:USERNAME `
  -LogonType Interactive `
  -RunLevel Highest

Register-ScheduledTask `
  -TaskName $TaskName `
  -Action $Action `
  -Trigger $Trigger `
  -Settings $Settings `
  -Principal $Principal `
  -Force | Out-Null

# 立刻啟動伺服器（如果還沒跑）
$Port = 8008
$already = Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if (-not $already) {
  Start-Process "wscript.exe" -ArgumentList "`"$VbsFile`""
}

Add-Type -AssemblyName System.Windows.Forms
[System.Windows.Forms.MessageBox]::Show(
  "✅ 設定完成！`n`n下次登入 Windows 後，伺服器會自動在背景啟動。`n點捷徑就能直接開網頁，不需等待。",
  "StockAI 自動啟動",
  "OK",
  "Information"
) | Out-Null
