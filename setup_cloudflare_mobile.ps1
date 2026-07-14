[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)]
  [ValidatePattern('^(?=.{4,253}$)(?!-)(?:[a-zA-Z0-9-]+\.)+[a-zA-Z]{2,63}$')]
  [string]$Hostname,

  [Parameter(Mandatory = $true)]
  [switch]$AccessConfirmed
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Cloudflared = "C:\Program Files (x86)\cloudflared\cloudflared.exe"
$TunnelId = "50f56c4a-1e9c-4c94-81f7-df7faef48508"
$TunnelName = "StockSystem-Mobile"
$CredentialPath = Join-Path $env:USERPROFILE ".cloudflared\$TunnelId.json"
$ConfigDirectory = Join-Path $Root "cloudflare"
$ConfigPath = Join-Path $ConfigDirectory "mobile_tunnel.yml"
$SettingsPath = Join-Path $ConfigDirectory "mobile_tunnel.json"
$StarterPath = Join-Path $Root "start_cloudflare_mobile.ps1"
$TunnelRunnerPath = Join-Path $ConfigDirectory "run_mobile_tunnel_hidden.vbs"
$TunnelTaskName = "StockSystem-CloudflareTunnel"
$TunnelTaskLog = Join-Path $ConfigDirectory "mobile_tunnel.task.log"

if (-not $AccessConfirmed) {
  throw "Cloudflare Access must be configured for this hostname before publishing."
}
if (-not (Test-Path -LiteralPath $Cloudflared)) {
  throw "cloudflared was not found: $Cloudflared"
}
if (-not (Test-Path -LiteralPath $CredentialPath)) {
  throw "Tunnel credential was not found: $CredentialPath"
}
if (-not (Test-Path -LiteralPath $TunnelRunnerPath)) {
  throw "Hidden tunnel runner was not found: $TunnelRunnerPath"
}
if (-not (Test-Path -LiteralPath $ConfigDirectory)) {
  New-Item -ItemType Directory -Path $ConfigDirectory | Out-Null
}

$normalizedHostname = $Hostname.Trim().TrimEnd('.').ToLowerInvariant()
$normalizedCredentialPath = $CredentialPath.Replace('\', '/')
$config = @"
tunnel: $TunnelId
credentials-file: $normalizedCredentialPath
protocol: http2
ingress:
  - hostname: $normalizedHostname
    service: http://127.0.0.1:8008
  - service: http_status:404
"@
Set-Content -LiteralPath $ConfigPath -Value $config -Encoding UTF8

& $Cloudflared tunnel --config $ConfigPath ingress validate
if ($LASTEXITCODE -ne 0) {
  throw "Cloudflare mobile ingress validation failed; DNS was not changed."
}

& $Cloudflared tunnel route dns $TunnelId $normalizedHostname
if ($LASTEXITCODE -ne 0) {
  throw "Cloudflare DNS route creation failed; mobile tunnel remains disabled."
}

$settings = [ordered]@{
  enabled = $true
  tunnelId = $TunnelId
  tunnelName = $TunnelName
  hostname = $normalizedHostname
  accessConfirmedAt = (Get-Date).ToString("yyyy-MM-ddTHH:mm:sszzz")
}
$settings | ConvertTo-Json | Set-Content -LiteralPath $SettingsPath -Encoding UTF8

$taskAction = New-ScheduledTaskAction `
  -Execute (Join-Path $env:SystemRoot "System32\wscript.exe") `
  -Argument ("//B //NoLogo `"{0}`"" -f $TunnelRunnerPath) `
  -WorkingDirectory $Root
$taskSettings = New-ScheduledTaskSettingsSet `
  -AllowStartIfOnBatteries `
  -DontStopIfGoingOnBatteries `
  -StartWhenAvailable `
  -RestartCount 999 `
  -RestartInterval (New-TimeSpan -Minutes 1) `
  -ExecutionTimeLimit ([TimeSpan]::Zero) `
  -Hidden `
  -MultipleInstances IgnoreNew
$taskUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$taskPrincipal = New-ScheduledTaskPrincipal `
  -UserId $taskUser `
  -LogonType Interactive `
  -RunLevel Limited
$taskTriggers = @(
  New-ScheduledTaskTrigger -AtLogOn -User $taskUser
  New-ScheduledTaskTrigger `
    -Weekly `
    -WeeksInterval 1 `
    -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday `
    -At "08:00"
)
Register-ScheduledTask `
  -TaskName $TunnelTaskName `
  -Action $taskAction `
  -Trigger $taskTriggers `
  -Settings $taskSettings `
  -Principal $taskPrincipal `
  -Description "StockSystem Cloudflare mobile tunnel" `
  -Force | Out-Null

& $StarterPath
Write-Host "Cloudflare mobile access is enabled: https://$normalizedHostname/"
