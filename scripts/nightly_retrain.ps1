# nightly_retrain.ps1 — 板読み nightly retrain (DESIGN 決定 12) の薄いラッパ
#
# 呼び出し元: _bellwether\scripts\daily_ops\daily_evening_pipeline.ps1 STEP 4
#   (引け後 16:40。exit 3 は「録画不足など — 前日 champion 継続」の WARN 扱い・非致命)
# 手動: .\scripts\nightly_retrain.ps1 [-Date 2026-07-16]
#
# exit code: 0 = 昇格 / 3 = champion 継続 (回復可能) / 1 = ハードエラー
param(
    [string]$Date
)
$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

# python の解決は start_runtime.ps1 と同じ (uv run — .venv を uv が解決する)
$runArgs = @('run', 'python', 'scripts/nightly_retrain.py')
if ($Date) { $runArgs += @('--date', $Date) }

& uv @runArgs
exit $LASTEXITCODE
