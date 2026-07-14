$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Cloudflared = "C:\Program Files (x86)\cloudflared\cloudflared.exe"
$SettingsPath = Join-Path $Root "cloudflare\mobile_tunnel.json"
$ConfigPath = Join-Path $Root "cloudflare\mobile_tunnel.yml"
$StdoutLog = Join-Path $Root "cloudflare\mobile_tunnel.out.log"
$StderrLog = Join-Path $Root "cloudflare\mobile_tunnel.err.log"
$LocalHealthUrl = "http://127.0.0.1:8008/"
$TunnelTaskName = "StockSystem-CloudflareTunnel"

if (-not (Test-Path -LiteralPath $SettingsPath)) { return }
$settings = Get-Content -LiteralPath $SettingsPath -Raw -Encoding UTF8 | ConvertFrom-Json
if ($settings.enabled -ne $true) { return }
if ([string]::IsNullOrWhiteSpace([string]$settings.hostname)) {
  throw "Cloudflare mobile hostname is missing."
}
if (-not (Test-Path -LiteralPath $Cloudflared)) {
  throw "cloudflared was not found: $Cloudflared"
}
if (-not (Test-Path -LiteralPath $ConfigPath)) {
  throw "Cloudflare mobile config was not found: $ConfigPath"
}

try {
  $response = Invoke-WebRequest -Uri $LocalHealthUrl -UseBasicParsing -TimeoutSec 3
  if ($response.StatusCode -lt 200 -or $response.StatusCode -ge 500) {
    throw "Local StockAI returned HTTP $($response.StatusCode)."
  }
} catch {
  throw "Local StockAI is not ready; mobile tunnel was not started: $($_.Exception.Message)"
}

$configPattern = [regex]::Escape($ConfigPath)
$tunnelPattern = [regex]::Escape([string]$settings.tunnelId)
$existing = Get-CimInstance Win32_Process -Filter "Name = 'cloudflared.exe'" -ErrorAction SilentlyContinue |
  Where-Object {
    $_.CommandLine -match $configPattern -or
    ($_.CommandLine -match "\btunnel\b" -and $_.CommandLine -match $tunnelPattern)
  } |
  Select-Object -First 1
if ($existing) { return }

& $Cloudflared tunnel --config $ConfigPath ingress validate
if ($LASTEXITCODE -ne 0) {
  throw "Cloudflare mobile ingress validation failed."
}

# The main launcher can itself run inside Task Scheduler. A cloudflared child
# started from that task is terminated when the launcher task completes, so use
# a dedicated long-running task when it is installed.
$tunnelTask = Get-ScheduledTask -TaskName $TunnelTaskName -ErrorAction SilentlyContinue
if ($tunnelTask) {
  Start-ScheduledTask -TaskName $TunnelTaskName
  $deadline = (Get-Date).AddSeconds(15)
  do {
    Start-Sleep -Milliseconds 500
    $existing = Get-CimInstance Win32_Process -Filter "Name = 'cloudflared.exe'" -ErrorAction SilentlyContinue |
      Where-Object {
        $_.CommandLine -match $configPattern -or
        ($_.CommandLine -match "\btunnel\b" -and $_.CommandLine -match $tunnelPattern)
      } |
      Select-Object -First 1
  } while (-not $existing -and (Get-Date) -lt $deadline)

  if (-not $existing) {
    $taskInfo = Get-ScheduledTaskInfo -TaskName $TunnelTaskName -ErrorAction SilentlyContinue
    throw "Cloudflare mobile tunnel task did not start (last result: $($taskInfo.LastTaskResult))."
  }
  return
}

$process = Start-Process `
  -FilePath $Cloudflared `
  -ArgumentList @("tunnel", "--config", $ConfigPath, "run", [string]$settings.tunnelId) `
  -WorkingDirectory $Root `
  -WindowStyle Hidden `
  -RedirectStandardOutput $StdoutLog `
  -RedirectStandardError $StderrLog `
  -PassThru

Start-Sleep -Seconds 2
if ($process.HasExited) {
  $detail = ""
  if (Test-Path -LiteralPath $StderrLog) {
    $detail = (Get-Content -LiteralPath $StderrLog -Tail 8 -Encoding UTF8) -join " | "
  }
  throw "Cloudflare mobile tunnel exited during startup. $detail"
}
