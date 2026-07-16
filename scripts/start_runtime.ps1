# scalp-agent 統合ランタイム起動 (毎朝 owner が kabu 本体ログイン後に手動実行 — DESIGN 決定 11)
#
#   .\scripts\start_runtime.ps1                 # 通常運転 (録画 + ペーパー、15:35 自己終了)
#   .\scripts\start_runtime.ps1 -RecordOnly     # 録画のみ (モデル不要)
#   .\scripts\start_runtime.ps1 -DurationMin 3 -IgnoreWindow   # smoke
#
# 注意:
# - 08:45 頃までに起動して寄付きから録画するのが理想
# - 稼働中に他プロセスが POST /token を発行しないこと (token 失効 → 録画即死)
# - パスワードは backcast/.env の PROD_KABU_API_PASSWORD (本書・repo に値を書かない)
param(
    [switch]$RecordOnly,
    [double]$DurationMin,
    [switch]$IgnoreWindow
)
$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
# backcast/.env の在処は端末ごとに違うので直書きしない (2026-07-16 に C:\Users\sasai へ
# 直書きし、kabu 本体が動いている D: 機で板読みが起動不能になった)。BACKCAST_ENV_FILE で上書き可
$envFile = $env:BACKCAST_ENV_FILE
if (-not $envFile) {
    foreach ($c in @('D:\Documents\backcast\.env', 'C:\Users\sasai\Documents\backcast\.env')) {
        if (Test-Path $c) { $envFile = $c; break }
    }
}
if (-not $envFile -or -not (Test-Path $envFile)) {
    Write-Error 'backcast\.env が見つからない (BACKCAST_ENV_FILE で明示指定できる)'; exit 2
}
$line = Select-String -Path $envFile -Pattern '^PROD_KABU_API_PASSWORD=(.+)$' | Select-Object -First 1
if (-not $line) { Write-Error "PROD_KABU_API_PASSWORD が $envFile に見つからない"; exit 2 }
$env:KABU_API_PASSWORD = $line.Matches[0].Groups[1].Value

# 多重起動ガード: 既存ランタイムがいる状態で起動すると token を奪い既存を殺す
$existing = Get-CimInstance Win32_Process -Filter "Name like 'python%'" |
    Where-Object { $_.CommandLine -match 'scalp_agent\.runtime\.runner' }
if ($existing) {
    Write-Error "scalp_agent.runtime.runner が既に稼働中 (PID $($existing.ProcessId -join ','))。多重起動は token 失効を招くため中止。"
    exit 3
}

$runArgs = @('run', 'python', '-m', 'scalp_agent.runtime.runner')
if ($RecordOnly) { $runArgs += '--record-only' }
if ($PSBoundParameters.ContainsKey('DurationMin')) { $runArgs += @('--duration-min', $DurationMin) }
if ($IgnoreWindow) { $runArgs += '--ignore-window' }

Set-Location $repo
& uv @runArgs
exit $LASTEXITCODE
