# 지금 바로 갱신하고 화면 열기
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')
$py = Find-Python
if (-not $py) { Write-Host '파이썬을 찾지 못했습니다. install.bat을 먼저 실행하세요.' -ForegroundColor Red; exit 1 }
Push-Location $Root
try {
    if ($args.Count -gt 0) { & $py -m analyzer analyze @args } else { & $py -m analyzer update-all }
} finally { Pop-Location }
if ((Test-Server) -and -not (Test-ServerVersion).ok) { Stop-Server }   # 옛 버전 서버가 떠 있으면 새 코드로 교체
if (Start-Server $py) { Start-Process $Url }
else { Write-Host "화면 서버가 켜지지 않았습니다. 바탕화면 '재무분석 열기.bat'을 실행하면 원인을 보여 줍니다." -ForegroundColor Red; Read-Host 'Enter' }
