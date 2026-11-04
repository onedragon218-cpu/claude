# 지금 바로 갱신하고 화면 열기
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')
$py = Find-Python
if (-not $py) { Write-Host '파이썬을 찾지 못했습니다. install.bat을 먼저 실행하세요.' -ForegroundColor Red; exit 1 }
Push-Location $Root
try {
    if ($args.Count -gt 0) { & $py -m analyzer analyze @args } else { & $py -m analyzer update-all }
} finally { Pop-Location }
Start-Server $py
Start-Process $Url
