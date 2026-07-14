param(
    [switch]$ProbeOnly
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
Add-Type -AssemblyName System.Windows.Forms
if (-not ("CapitalComEventState" -as [type])) {
    Add-Type -TypeDefinition @"
using System;
using System.Collections.Concurrent;

public static class CapitalComEventState
{
    public static readonly ConcurrentQueue<string> QuoteEvents = new ConcurrentQueue<string>();
    public static int ConnectionKind = -1;
    public static int ConnectionCode = -1;
    public static int StocksReady = 0;

    public static void Reset()
    {
        string ignored;
        while (QuoteEvents.TryDequeue(out ignored)) { }
        ConnectionKind = -1;
        ConnectionCode = -1;
        StocksReady = 0;
    }

    public static void OnReplyMessage(string userId, string message, ref short confirmCode)
    {
        confirmCode = -1;
    }

    public static void OnConnection(int kind, int code)
    {
        ConnectionKind = kind;
        ConnectionCode = code;
        if (kind == 3003 && code == 0) { System.Threading.Interlocked.Exchange(ref StocksReady, 1); }
    }

    public static void OnNotifyQuoteLONG(short marketNo, int stockIndex)
    {
        QuoteEvents.Enqueue(marketNo.ToString() + ":" + stockIndex.ToString());
    }

}
"@
}

function Wait-ComEvent {
    param([int]$Milliseconds = 100)
    [System.Windows.Forms.Application]::DoEvents()
    Start-Sleep -Milliseconds $Milliseconds
    [System.Windows.Forms.Application]::DoEvents()
}

function Write-Result {
    param([hashtable]$Payload)
    [Console]::Out.WriteLine(($Payload | ConvertTo-Json -Compress -Depth 6))
    exit 0
}

function Invoke-ComMethod {
    param(
        [object]$Instance,
        [Type]$InstanceType,
        [string]$Name,
        [object[]]$Arguments = @()
    )
    $method = $InstanceType.GetMethod($Name)
    if ($null -eq $method) {
        throw "Capital COM method not found: $Name"
    }
    return $method.Invoke($Instance, $Arguments)
}

function Get-CodeMessage {
    param([object]$Center, [Type]$CenterType, [int]$Code)
    try {
        return [string](Invoke-ComMethod $Center $CenterType "SKCenterLib_GetReturnCodeMessage" @($Code))
    } catch {
        return ""
    }
}

function Get-ConnectionMessage {
    param([object]$Center, [Type]$CenterType, [int]$Code)
    if ($Code -eq 0) { return "disconnected" }
    if ($Code -eq 1) { return "connected" }
    if ($Code -eq 2) { return "downloading" }
    return Get-CodeMessage $Center $CenterType $Code
}

$quote = $null
$reply = $null
$replyHandler = $null
$quoteConnectionHandler = $null
$quoteNotificationHandler = $null
try {
    $capitalHome = [string]${env:CAPITAL_API_HOME}
    if ([string]::IsNullOrWhiteSpace($capitalHome)) { $capitalHome = "C:\CapitalAPI" }
    $interopPath = Join-Path $capitalHome "Interop.SKCOMLib.dll"
    if (-not (Test-Path -LiteralPath $interopPath -PathType Leaf)) {
        Write-Result @{ ok = $false; usable = $false; error = "Capital Interop.SKCOMLib.dll was not found" }
    }

    # Loading from bytes avoids the downloaded-file Zone.Identifier error
    # 0x80131515. COM objects still come from the registered SKCOM.dll.
    $assemblyBytes = [System.IO.File]::ReadAllBytes($interopPath)
    $assembly = [System.Reflection.Assembly]::Load($assemblyBytes)
    $centerType = $assembly.GetType("SKCOMLib.SKCenterLibClass", $true)
    $quoteType = $assembly.GetType("SKCOMLib.SKQuoteLibClass", $true)
    $replyType = $assembly.GetType("SKCOMLib.SKReplyLibClass", $true)
    $stockType = $assembly.GetType("SKCOMLib.SKSTOCKLONG", $true)
    $center = [Activator]::CreateInstance($centerType)
    $quote = [Activator]::CreateInstance($quoteType)
    $reply = [Activator]::CreateInstance($replyType)

    [CapitalComEventState]::Reset()

    # Capital requires OnReplyMessage to be registered before Login. Event
    # delegates are synchronous so the by-ref confirmation code is really -1.
    $replyDelegateType = $assembly.GetType("SKCOMLib._ISKReplyLibEvents_OnReplyMessageEventHandler", $true)
    $replyHandler = [Delegate]::CreateDelegate(
        $replyDelegateType, [CapitalComEventState].GetMethod("OnReplyMessage")
    )
    [void]$replyType.GetMethod("add_OnReplyMessage").Invoke($reply, @($replyHandler))

    $connectionDelegateType = $assembly.GetType("SKCOMLib._ISKQuoteLibEvents_OnConnectionEventHandler", $true)
    $quoteConnectionHandler = [Delegate]::CreateDelegate(
        $connectionDelegateType, [CapitalComEventState].GetMethod("OnConnection")
    )
    [void]$quoteType.GetMethod("add_OnConnection").Invoke($quote, @($quoteConnectionHandler))

    $notificationDelegateType = $assembly.GetType("SKCOMLib._ISKQuoteLibEvents_OnNotifyQuoteLONGEventHandler", $true)
    $quoteNotificationHandler = [Delegate]::CreateDelegate(
        $notificationDelegateType, [CapitalComEventState].GetMethod("OnNotifyQuoteLONG")
    )
    [void]$quoteType.GetMethod("add_OnNotifyQuoteLONG").Invoke($quote, @($quoteNotificationHandler))

    if ($ProbeOnly) {
        $connectionCode = [int](Invoke-ComMethod $quote $quoteType "SKQuoteLib_IsConnected")
        Write-Result @{
            ok = $true
            usable = $false
            comReady = $true
            quoteConnectionCode = $connectionCode
            quoteConnectionMessage = Get-ConnectionMessage $center $centerType $connectionCode
        }
    }

    $requestText = [Console]::In.ReadToEnd()
    $request = $requestText | ConvertFrom-Json
    $userId = [string]$request.userId
    $password = [string]$request.password
    $symbols = @()
    foreach ($value in @($request.symbols)) {
        $clean = ([string]$value).Trim()
        if ($clean -match '^[0-9A-Za-z]{2,12}$' -and $symbols -notcontains $clean) {
            $symbols += $clean
        }
    }
    if ($symbols.Count -eq 0) {
        $symbol = ([string]$request.symbol).Trim()
        if ([string]::IsNullOrWhiteSpace($symbol)) { $symbol = "2330" }
        $symbols = @($symbol)
    }
    $symbols = @($symbols | Select-Object -First 100)
    if ([string]::IsNullOrWhiteSpace($userId) -or [string]::IsNullOrWhiteSpace($password)) {
        Write-Result @{ ok = $false; configured = $false; usable = $false; error = "Capital login credentials are not configured" }
    }

    $loginCode = [int](Invoke-ComMethod $center $centerType "SKCenterLib_Login" @($userId, $password))
    $loginMessage = Get-CodeMessage $center $centerType $loginCode
    if ($loginCode -ne 0) {
        Write-Result @{
            ok = $false
            configured = $true
            usable = $false
            loginCode = $loginCode
            loginMessage = $loginMessage
            error = "Capital API login failed"
        }
    }

    $monitorCode = [int](Invoke-ComMethod $quote $quoteType "SKQuoteLib_EnterMonitorLONG")
    $connectionCode = [int](Invoke-ComMethod $quote $quoteType "SKQuoteLib_IsConnected")
    $deadline = [DateTime]::UtcNow.AddSeconds(20)
    while ($connectionCode -notin @(1, 2) -and [DateTime]::UtcNow -lt $deadline) {
        Wait-ComEvent 100
        $connectionCode = [int](Invoke-ComMethod $quote $quoteType "SKQuoteLib_IsConnected")
    }
    $connectionMessage = Get-ConnectionMessage $center $centerType $connectionCode
    if ($monitorCode -ne 0 -or $connectionCode -notin @(1, 2)) {
        Write-Result @{
            ok = $false
            configured = $true
            usable = $false
            loginCode = $loginCode
            loginMessage = $loginMessage
            monitorCode = $monitorCode
            quoteConnectionCode = $connectionCode
            quoteConnectionMessage = $connectionMessage
            error = "Capital quote connection was not established"
        }
    }

    $stocksReadyDeadline = [DateTime]::UtcNow.AddSeconds(45)
    while ([CapitalComEventState]::StocksReady -ne 1 -and [DateTime]::UtcNow -lt $stocksReadyDeadline) {
        Wait-ComEvent 100
    }
    if ([CapitalComEventState]::StocksReady -ne 1) {
        Write-Result @{
            ok = $false
            configured = $true
            usable = $false
            loginCode = $loginCode
            loginMessage = $loginMessage
            monitorCode = $monitorCode
            quoteConnectionCode = $connectionCode
            quoteConnectionMessage = $connectionMessage
            connectionEventKind = [CapitalComEventState]::ConnectionKind
            connectionEventCode = [CapitalComEventState]::ConnectionCode
            stocksReady = $false
            error = "Capital stock catalog did not become ready"
        }
    }

    # Capital documents require page 1 and allow at most 100 symbols on one
    # SKQuoteLib_RequestStocks subscription.
    $page = [int16]1
    $symbolText = $symbols -join ","
    $requestCode = -1
    $subscribeDeadline = [DateTime]::UtcNow.AddSeconds(25)
    while ($requestCode -ne 0 -and [DateTime]::UtcNow -lt $subscribeDeadline) {
        $requestArguments = [object[]]@($page, $symbolText)
        $requestCode = [int](Invoke-ComMethod $quote $quoteType "SKQuoteLib_RequestStocks" $requestArguments)
        if ($requestCode -ne 0) { Wait-ComEvent 200 }
    }
    if ($requestCode -ne 0) {
        Write-Result @{
            ok = $false
            configured = $true
            usable = $false
            loginCode = $loginCode
            monitorCode = $monitorCode
            quoteConnectionCode = $connectionCode
            requestCode = $requestCode
            requestMessage = Get-CodeMessage $center $centerType $requestCode
            error = "Capital stock subscription failed"
        }
    }

    $quotes = @{}
    $stockCodes = @{}
    $pending = @{}
    foreach ($requestedSymbol in $symbols) { $pending[$requestedSymbol] = $true }
    $quoteDeadline = [DateTime]::UtcNow.AddSeconds(12)
    while ($pending.Count -gt 0 -and [DateTime]::UtcNow -lt $quoteDeadline) {
        Wait-ComEvent 100
        $eventKey = $null
        while ([CapitalComEventState]::QuoteEvents.TryDequeue([ref]$eventKey)) {
            $eventParts = ([string]$eventKey).Split(":")
            if ($eventParts.Count -ne 2) { continue }
            $marketNo = [int16]$eventParts[0]
            $stockIndex = [int]$eventParts[1]
            $stockArguments = [object[]]@($marketNo, $stockIndex, [Activator]::CreateInstance($stockType))
            $stockCode = [int](Invoke-ComMethod $quote $quoteType "SKQuoteLib_GetStockByIndexLONG" $stockArguments)
            if ($stockCode -ne 0) { continue }

            $stock = $stockArguments[2]
            $quoteSymbol = ([string]$stockType.GetField("bstrStockNo").GetValue($stock)).Trim()
            $stockCodes[$quoteSymbol] = $stockCode
            if (-not $pending.ContainsKey($quoteSymbol)) { continue }
            $decimal = [Math]::Min(6, [Math]::Max(0, [int]$stockType.GetField("sDecimal").GetValue($stock)))
            $divisor = [Math]::Pow(10, $decimal)
            $closeRaw = [int]$stockType.GetField("nClose").GetValue($stock)
            $referenceRaw = [int]$stockType.GetField("nRef").GetValue($stock)
            $tradingDay = [int]$stockType.GetField("nTradingDay").GetValue($stock)
            $dealTime = [int]$stockType.GetField("nDealTime").GetValue($stock)
            if ($closeRaw -le 0 -or $tradingDay -le 0) { continue }

            $price = [Math]::Round($closeRaw / $divisor, $decimal)
            $referencePrice = [Math]::Round($referenceRaw / $divisor, $decimal)
            $openPrice = [Math]::Round(([int]$stockType.GetField("nOpen").GetValue($stock)) / $divisor, $decimal)
            $highPrice = [Math]::Round(([int]$stockType.GetField("nHigh").GetValue($stock)) / $divisor, $decimal)
            $lowPrice = [Math]::Round(([int]$stockType.GetField("nLow").GetValue($stock)) / $divisor, $decimal)
            $bidPrice = [Math]::Round(([int]$stockType.GetField("nBid").GetValue($stock)) / $divisor, $decimal)
            $askPrice = [Math]::Round(([int]$stockType.GetField("nAsk").GetValue($stock)) / $divisor, $decimal)
            $dateText = "{0:D8}" -f $tradingDay
            $timeText = "{0:D6}" -f $dealTime
            $quoteDate = "{0}-{1}-{2}" -f $dateText.Substring(0, 4), $dateText.Substring(4, 2), $dateText.Substring(6, 2)
            $quoteTime = "{0}:{1}:{2}" -f $timeText.Substring(0, 2), $timeText.Substring(2, 2), $timeText.Substring(4, 2)
            $changeRate = $null
            if ($referencePrice -gt 0) { $changeRate = [Math]::Round((($price / $referencePrice) - 1) * 100, 2) }
            $quotes[$quoteSymbol] = @{
                code = $quoteSymbol
                name = ([string]$stockType.GetField("bstrStockName").GetValue($stock)).Trim()
                currentPrice = $price
                referencePrice = $referencePrice
                open = $openPrice
                high = $highPrice
                low = $lowPrice
                bidPrice = $bidPrice
                askPrice = $askPrice
                changeRate = $changeRate
                totalVolume = [int]$stockType.GetField("nTQty").GetValue($stock)
                tradingDay = $tradingDay
                dealTime = $dealTime
                quoteDate = $quoteDate
                quoteTime = $quoteTime
                quoteTimestamp = "$quoteDate $quoteTime"
                receivedAt = [DateTimeOffset]::Now.ToString("o")
                source = "Capital Strategy King COM"
                eventConfirmed = $true
            }
            $pending.Remove($quoteSymbol)
        }
    }

    $missing = @($symbols | Where-Object { -not $quotes.ContainsKey($_) })
    $usable = $quotes.Count -gt 0
    $result = @{
        ok = $usable
        configured = $true
        usable = $usable
        loginCode = $loginCode
        loginMessage = $loginMessage
        monitorCode = $monitorCode
        quoteConnectionCode = $connectionCode
        quoteConnectionMessage = $connectionMessage
        requestCode = $requestCode
        connectionEventKind = [CapitalComEventState]::ConnectionKind
        connectionEventCode = [CapitalComEventState]::ConnectionCode
        stocksReady = $true
        requested = $symbols.Count
        count = $quotes.Count
        quotes = $quotes
        missingSymbols = $missing
        source = "Capital Strategy King COM"
        quoteEventConfirmed = $usable
        error = $(if ($usable) { "" } else { "Capital connected but did not return a valid stock quote" })
    }
    if ($symbols.Count -eq 1 -and $quotes.ContainsKey($symbols[0])) {
        $single = $quotes[$symbols[0]]
        $result.symbol = $single.code
        $result.stockName = $single.name
        $result.price = $single.currentPrice
        $result.referencePrice = $single.referencePrice
        $result.totalVolume = $single.totalVolume
        $result.tradingDay = $single.tradingDay
        $result.dealTime = $single.dealTime
        $result.quoteTimestamp = $single.quoteTimestamp
        $result.stockCode = $stockCodes[$symbols[0]]
    }
    Write-Result $result
} catch {
    Write-Result @{
        ok = $false
        configured = $true
        usable = $false
        error = "Capital real quote test failed: $($_.Exception.GetType().Name)"
    }
} finally {
    if ($null -ne $reply -and $null -ne $replyHandler) {
        try {
            [void]$replyType.GetMethod("remove_OnReplyMessage").Invoke($reply, @($replyHandler))
        } catch {
        }
    }
    if ($null -ne $quote -and $null -ne $quoteNotificationHandler) {
        try {
            [void]$quoteType.GetMethod("remove_OnNotifyQuoteLONG").Invoke($quote, @($quoteNotificationHandler))
        } catch {
        }
    }
    if ($null -ne $quote -and $null -ne $quoteConnectionHandler) {
        try {
            [void]$quoteType.GetMethod("remove_OnConnection").Invoke($quote, @($quoteConnectionHandler))
        } catch {
        }
    }
    if ($null -ne $quote) {
        try {
            $quoteType = $quote.GetType()
            [void](Invoke-ComMethod $quote $quoteType "SKQuoteLib_LeaveMonitor")
        } catch {
        }
    }
}
