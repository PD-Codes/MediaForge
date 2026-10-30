param(
    [ValidateSet('Start', 'Stop', 'Remove')]
    [string]$Action = 'Start',
    [string]$EnvironmentRoot = $PSScriptRoot,
    [ValidateRange(1024, 65535)]
    [int]$Port = 18080
)

$ErrorActionPreference = 'Stop'
$testRoot = (Resolve-Path -LiteralPath $EnvironmentRoot).Path
$marker = Join-Path $testRoot '.mediaforge-test'
if (-not (Test-Path -LiteralPath $marker) -or
    (Get-Content -LiteralPath $marker -Raw).Trim() -ne 'MediaForge disposable test environment') {
    throw 'Dieser Ordner ist keine gekennzeichnete MediaForge-Testumgebung.'
}
if ($testRoot -eq [IO.Path]::GetPathRoot($testRoot)) {
    throw 'Ein Laufwerksstamm darf keine Testumgebung sein.'
}
$python = Join-Path $testRoot 'venv\Scripts\python.exe'
$pidFile = Join-Path $testRoot 'process.json'
$appProcess = $null
if (Test-Path -LiteralPath $pidFile) {
    $savedProcess = Get-Content -LiteralPath $pidFile -Raw | ConvertFrom-Json
    $candidate = Get-Process -Id $savedProcess.Id -ErrorAction SilentlyContinue
    if ($candidate) {
        if ($candidate.Path -ne $python -or
            $candidate.StartTime.ToUniversalTime().Ticks.ToString() -ne $savedProcess.StartTicks) {
            throw 'Die gespeicherte Prozess-ID gehoert nicht mehr zur Testinstanz.'
        }
        $appProcess = $candidate
    }
}

if ($Action -eq 'Start') {
    if ($appProcess) {
        Write-Output "Testinstanz laeuft bereits: http://127.0.0.1:$Port"
        return
    }
    $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, $Port)
    try { $listener.Start() } finally { $listener.Stop() }
    $variables = @{
        PYTHONPATH = (Join-Path $testRoot 'source\src')
        PYTHONDONTWRITEBYTECODE = '1'
        PYTHONUTF8 = '1'
        MEDIAFORGE_CONFIG_DIR = (Join-Path $testRoot 'data')
        MEDIAFORGE_DOWNLOAD_PATH = (Join-Path $testRoot 'downloads')
        MEDIAFORGE_NO_DOMAIN_LOOKUP = '1'
        MEDIAFORGE_DEBUG_MODE = '0'
        MEDIAFORGE_WEB_HOST = '127.0.0.1'
        MEDIAFORGE_WEB_PORT = "$Port"
        MEDIAFORGE_WEB_THREADS = '8'
        APPDATA = (Join-Path $testRoot 'appdata')
        LOCALAPPDATA = (Join-Path $testRoot 'localappdata')
        PLAYWRIGHT_BROWSERS_PATH = (Join-Path $testRoot 'browsers')
        TEMP = (Join-Path $testRoot 'temp')
        TMP = (Join-Path $testRoot 'temp')
    }
    $previous = @{}
    try {
        foreach ($name in $variables.Keys) {
            $previous[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
            [Environment]::SetEnvironmentVariable($name, $variables[$name], 'Process')
        }
        $command = '"from mediaforge.web.app import start_web_ui; start_web_ui(host=''127.0.0.1'', port=' + $Port + ', open_browser=False)"'
        $appProcess = Start-Process -FilePath $python -ArgumentList @('-c', $command) `
            -WorkingDirectory $testRoot -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput (Join-Path $testRoot 'stdout.log') `
            -RedirectStandardError (Join-Path $testRoot 'stderr.log')
        @{
            Id = $appProcess.Id
            StartTicks = $appProcess.StartTime.ToUniversalTime().Ticks.ToString()
        } | ConvertTo-Json | Set-Content -LiteralPath $pidFile -Encoding UTF8
    } finally {
        foreach ($name in $previous.Keys) {
            [Environment]::SetEnvironmentVariable($name, $previous[$name], 'Process')
        }
    }
    $deadline = [DateTime]::UtcNow.AddSeconds(45)
    while ([DateTime]::UtcNow -lt $deadline) {
        $appProcess.Refresh()
        if ($appProcess.HasExited) {
            throw 'Start fehlgeschlagen. Details stehen in stderr.log im Testordner.'
        }
        try {
            $response = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/healthz" `
                -UseBasicParsing -TimeoutSec 2
            if ($response.StatusCode -eq 200) {
                Write-Output "Testinstanz erreichbar: http://127.0.0.1:$Port"
                return
            }
        } catch { }
        Start-Sleep -Milliseconds 500
    }
    Stop-Process -Id $appProcess.Id
    throw 'Die Testinstanz wurde nicht rechtzeitig erreichbar. Sie wurde wieder beendet.'
}

if ($appProcess) {
    Stop-Process -Id $appProcess.Id
    if (-not $appProcess.WaitForExit(10000)) { throw 'Die Testinstanz wurde nicht beendet.' }
}
if (Test-Path -LiteralPath $pidFile) { Remove-Item -LiteralPath $pidFile }
if ($Action -eq 'Remove') {
    Set-Location -LiteralPath (Split-Path -Parent $testRoot)
    Remove-Item -LiteralPath $testRoot -Recurse -Force
    Write-Output 'Die Testumgebung wurde entfernt.'
} else {
    Write-Output 'Die Testinstanz wurde beendet.'
}
