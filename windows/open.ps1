# 화면 열기: 서버가 꺼져 있으면 켜고 브라우저를 연다. 켜지지 않으면 원인을 진단해 보여 준다.
. (Join-Path $PSScriptRoot 'common.ps1')
$py = Find-Python
if (-not $py) { Write-Host '파이썬을 찾지 못했습니다. install.bat을 먼저 실행하세요.' -ForegroundColor Red; Read-Host 'Enter를 누르면 닫힙니다'; exit 1 }
if (-not (Test-Server)) {
    Write-Host '화면 서버를 켜는 중입니다...'
    $null = Start-Server $py
}
if (Test-Server) { Start-Process $Url; exit 0 }
Write-Host ''
Write-Host '화면 서버가 켜지지 않았습니다. 원인을 진단합니다.' -ForegroundColor Red
Push-Location $Root
try { & $py -m analyzer doctor } finally { Pop-Location }
Write-Host ''
Write-Host '이제 이 창에서 서버를 직접 켭니다. 아래에 오류가 나오면 그 내용을 그대로 복사해 보내 주세요.' -ForegroundColor Yellow
Write-Host "서버가 켜지면 브라우저에서 $Url 을 여세요. 이 창을 닫으면 서버도 꺼집니다." -ForegroundColor Yellow
Push-Location $Root
try { & $py -m analyzer serve --port $Port } finally { Pop-Location }
Read-Host 'Enter를 누르면 닫힙니다'
