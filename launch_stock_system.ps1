$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$NoBrowser = $args -contains "-NoBrowser"
$PythonExe = "C:\Users\m05d1\AppData\Local\Python\pythoncore-3.14-64\python.exe"
$Port = 8008
$Url = "http://127.0.0.1:$Port/"
$StdoutLog = Join-Path $Root "server_stdout.log"
$StderrLog = Join-Path $Root "server_stderr.log"
$LauncherLog = Join-Path $Root "launcher.log"

function Write-LauncherLog([string]$Message) {
  try {
    Add-Content -LiteralPath $LauncherLog `
      -Value ("{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message) `
      -Encoding UTF8
  } catch {}
}

Write-LauncherLog "desktop launcher started"

function Repair-ProcessPathEnvironment {
  $keys = @([System.Environment]::GetEnvironmentVariables("Process").Keys |
    Where-Object { $_ -ieq "Path" })
  if ($keys.Count -le 1) { return }

  $pathValue = [System.Environment]::GetEnvironmentVariable("Path", "Process")
  if ([string]::IsNullOrWhiteSpace($pathValue)) {
    foreach ($key in $keys) {
      $candidate = [System.Environment]::GetEnvironmentVariable([string]$key, "Process")
      if (-not [string]::IsNullOrWhiteSpace($candidate)) {
        $pathValue = $candidate
        break
      }
    }
  }

  [System.Environment]::SetEnvironmentVariable("Path", $pathValue, "Process")
  foreach ($key in $keys) {
    if ([string]$key -cne "Path") {
      [System.Environment]::SetEnvironmentVariable([string]$key, $null, "Process")
    }
  }
}

Repair-ProcessPathEnvironment

function Get-StockServerConnection {
  Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
    Select-Object -First 1
}

function Test-StockServerHealth {
  try {
    $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 2
    return [bool]($response.StatusCode -ge 200 -and $response.StatusCode -lt 500)
  } catch {
    return $false
  }
}

if (-not (Test-Path -LiteralPath $PythonExe)) {
  Add-Type -AssemblyName System.Windows.Forms
  [System.Windows.Forms.MessageBox]::Show(
    "Fixed Python was not found:`n$PythonExe`nPlease check the Python 3.14 environment.",
    "StockAI startup failed",
    "OK",
    "Error"
  ) | Out-Null
  exit 1
}

# 使用者心急連點兩下桌面捷徑(或雙擊沒反應又點一次)會在幾秒內觸發兩個各自
# 獨立的 launch_stock_system.ps1 行程，兩邊都可能在「port還沒監聽」的瞬間
# 各自判斷要啟動，導致第二個 server.py 因 port 已被佔用而 bind 失敗(隱藏
# 視窗+分開的 log，使用者完全看不到)。用具名 Mutex 把「檢查->必要時啟動」
# 整段序列化，只用行程內建的.NET物件、不需要額外權限，同一個Windows使用者
# 階段內都能互斥。
$StartupMutex = $null
$acquiredMutex = $false
try {
  $StartupMutex = New-Object System.Threading.Mutex($false, "StockAI_Server_Startup_Lock")
  try {
    $acquiredMutex = $StartupMutex.WaitOne(60000)
  } catch [System.Threading.AbandonedMutexException] {
    # 前一個持有者沒有正常釋放就結束(例如被強制關閉)，.NET在這裡仍然把鎖
    # 判給我們，語意上鎖已經拿到了，不是要放棄整個啟動流程。
    $acquiredMutex = $true
  }
} catch {
  $acquiredMutex = $false
}

try {
  # 單例保護(2026-07-07):清掉「還在跑 server.py 但不是目前 8008 健康擁有者」的
  # 殭屍/重複行程。今天出過事——舊 server.py 丟了 8008 埠卻沒死、背景執行緒空轉狂燒
  # 一顆核心 44%(整台變卡),而下面那段舊守衛只看 8008 擁有者、完全抓不到它。這裡
  # 改用「命令列比對 server.py」把所有不是健康擁有者的 server 都殺掉,徹底根治雙開。
  $healthyOwnerPid = $null
  $probe = Get-StockServerConnection
  if ($probe -and (Test-StockServerHealth)) { $healthyOwnerPid = [int]$probe.OwningProcess }
  Get-CimInstance Win32_Process -Filter "Name LIKE 'python%'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -match '\bserver\.py\b' -and [int]$_.ProcessId -ne $healthyOwnerPid } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

  $connection = Get-StockServerConnection
  $isHealthy = $false
  if ($connection) {
    $process = Get-Process -Id $connection.OwningProcess -ErrorAction SilentlyContinue
    if ($process -and $process.Path -eq $PythonExe) {
      # Port 有人在聽不代表真的活著——GIL 長時間被佔用/死結時 TCP 連線仍然
      # 是 established，但 HTTP 請求永遠不會有回應，只看 TCP 層會誤判成
      # 健康，這裡改成真的送一次 HTTP 請求驗證。
      $isHealthy = Test-StockServerHealth
    }
    if (-not $isHealthy) {
      if ($process) {
        Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 2
      }
      $connection = $null
    }
  }

  if (-not $connection -or -not $isHealthy) {
    $healthProcess = Start-Process `
      -FilePath $PythonExe `
      -ArgumentList @("health_check.py", "--no-predict") `
      -WorkingDirectory $Root `
      -WindowStyle Hidden `
      -Wait `
      -PassThru

    if ($healthProcess.ExitCode -ne 0) {
      Start-Process `
        -FilePath $PythonExe `
        -ArgumentList @("-m", "pip", "install", "-r", "requirements-model.txt") `
        -WorkingDirectory $Root `
        -WindowStyle Hidden `
        -Wait | Out-Null
    }

    Start-Process `
      -FilePath $PythonExe `
      -ArgumentList @("server.py") `
      -WorkingDirectory $Root `
      -WindowStyle Hidden `
      -RedirectStandardOutput $StdoutLog `
      -RedirectStandardError $StderrLog

    # 在放開 Mutex 前等到伺服器真的開始回應，另一次啟動流程拿到鎖時重新
    # 檢查才會看到「已在跑」而跳過，不然兩邊都可能在對方的 server.py 還沒
    # bind 完成前就已經各自判斷「還沒啟動」而重複啟動一次。
    $deadline = (Get-Date).AddSeconds(60)
    while ((Get-Date) -lt $deadline) {
      if ((Get-StockServerConnection) -and (Test-StockServerHealth)) {
        break
      }
      Start-Sleep -Seconds 1
    }
  }
} finally {
  if ($acquiredMutex -and $StartupMutex) {
    try { $StartupMutex.ReleaseMutex() } catch {}
  }
  if ($StartupMutex) { $StartupMutex.Dispose() }
}

# Cloudflare 手機 Tunnel 只有在完成 Access 白名單並由
# setup_cloudflare_mobile.ps1 將 enabled 設為 true 後才會啟動。
# 啟動失敗不影響本機桌面版，但會留在 mobile_tunnel.err.log 供追查。
$MobileTunnelStarter = Join-Path $Root "start_cloudflare_mobile.ps1"
if (Test-Path -LiteralPath $MobileTunnelStarter) {
  try {
    & $MobileTunnelStarter
  } catch {
    Write-Warning ("Cloudflare mobile tunnel was not started: {0}" -f $_.Exception.Message)
  }
}

# 啟動流程已在上面等待伺服器健康，直接用 HTTP URL 交給系統預設瀏覽器。
# 這台電腦的 .html 檔案關聯仍指向已淘汰的 Internet Explorer；開 loader.html
# 會讓伺服器其實正常、桌面捷徑卻看起來完全沒反應。
if (-not (Test-StockServerHealth)) {
  Write-LauncherLog "server health check failed after startup wait"
  Add-Type -AssemblyName System.Windows.Forms
  [System.Windows.Forms.MessageBox]::Show(
    "股票看盤伺服器未能在 60 秒內啟動。`n請查看：$StderrLog",
    "股票看盤系統啟動失敗",
    "OK",
    "Error"
  ) | Out-Null
  exit 1
}

if ($NoBrowser) {
  Write-LauncherLog "server ready (no browser requested)"
  exit 0
}

try {
  $chrome = "C:\Program Files\Google\Chrome\Application\chrome.exe"
  if (Test-Path -LiteralPath $chrome) {
    Start-Process -FilePath $chrome -ArgumentList @("--new-window", $Url)
    Write-LauncherLog "opened dashboard with Chrome: $Url"
  } else {
    Start-Process $Url
    Write-LauncherLog "opened dashboard with default browser: $Url"
  }
} catch {
  Write-LauncherLog ("primary browser open failed: {0}" -f $_.Exception.Message)
  $browserPaths = @(
    "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    "C:\Program Files\Microsoft\Edge\Application\msedge.exe"
  )
  $browser = $browserPaths | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
  if ($browser) {
    Start-Process -FilePath $browser -ArgumentList $Url
    Write-LauncherLog "opened dashboard with fallback browser: $browser"
  } else {
    Write-LauncherLog "no usable browser was found"
    Add-Type -AssemblyName System.Windows.Forms
    [System.Windows.Forms.MessageBox]::Show(
      "伺服器已啟動，但找不到可開啟網頁的瀏覽器。`n網址：$Url",
      "股票看盤系統",
      "OK",
      "Warning"
    ) | Out-Null
  }
}
