# 태블릿 접속 설정: 같은 와이파이의 태블릿·휴대폰에서 화면을 비밀번호로 열 수 있게 켜거나 끈다.
. (Join-Path $PSScriptRoot 'common.ps1')
$py = Find-Python
if (-not $py) { Write-Host '파이썬을 찾지 못했습니다. install.bat을 먼저 실행하세요.' -ForegroundColor Red; Read-Host 'Enter를 누르면 닫힙니다'; exit 1 }
$RuleName = 'CorpAnalyzer 태블릿 접속'

function Invoke-Admin([string]$cmd) {
    # 관리자 권한이 필요한 명령(방화벽·네트워크 종류)만 따로 권한을 요청해 실행
    try { Start-Process powershell -Verb RunAs -Wait -WindowStyle Hidden -ArgumentList '-NoProfile', '-Command', $cmd; return $true }
    catch { Write-Host "      관리자 권한을 받지 못했습니다: $($_.Exception.Message)" -ForegroundColor Yellow; return $false }
}

Push-Location $Root
try { & $py -m analyzer lan status } finally { Pop-Location }
Write-Host ''
Write-Host '1) 켜기(비밀번호 새로 정하기)   2) 끄기   3) 그대로 두고 닫기'
$c = Read-Host '번호를 고르세요'

if ($c -eq '1') {
    $p1 = Read-Host '태블릿 접속 비밀번호(6자 이상)' -AsSecureString
    $p2 = Read-Host '한 번 더' -AsSecureString
    $t1 = [Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($p1))
    $t2 = [Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($p2))
    if ($t1 -ne $t2) { Write-Host '두 번 넣은 비밀번호가 다릅니다. 다시 실행하세요.' -ForegroundColor Red; Read-Host 'Enter를 누르면 닫힙니다'; exit 1 }
    if ($t1.Length -lt 6) { Write-Host '비밀번호는 6자 이상이어야 합니다.' -ForegroundColor Red; Read-Host 'Enter를 누르면 닫힙니다'; exit 1 }
    Push-Location $Root
    try { $t1 | & $py -m analyzer lan on --password-stdin | Out-Null } finally { Pop-Location }
    $t1 = $null; $t2 = $null

    # 방화벽: '개인' 네트워크에서만 8765 포트 허용
    if (-not (Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue)) {
        Write-Host '      방화벽에 8765 포트(개인 네트워크만)를 허용합니다. 권한 확인 창이 뜨면 [예]를 누르세요.' -ForegroundColor Cyan
        $null = Invoke-Admin "New-NetFirewallRule -DisplayName '$RuleName' -Direction Inbound -Protocol TCP -LocalPort $Port -Action Allow -Profile Private"
    }
    if (Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue) { Write-Host '      방화벽 허용: 완료' -ForegroundColor Green }
    else { Write-Host '      방화벽 허용을 하지 못했습니다. 태블릿에서 열리지 않으면 이 파일을 다시 실행해 [예]를 눌러 주세요.' -ForegroundColor Yellow }

    # 와이파이가 '공용' 네트워크면 방화벽 허용이 적용되지 않음 → 집 와이파이라면 '개인'으로 바꿀지 묻기
    $pub = Get-NetConnectionProfile -ErrorAction SilentlyContinue | Where-Object { $_.NetworkCategory -eq 'Public' }
    foreach ($n in $pub) {
        $a = Read-Host "      지금 네트워크 '$($n.Name)'가 '공용'으로 되어 있어 태블릿이 접속할 수 없습니다. 집 와이파이라면 '개인'으로 바꿀까요? (y/N)"
        if ($a -eq 'y') { $null = Invoke-Admin "Set-NetConnectionProfile -InterfaceIndex $($n.InterfaceIndex) -NetworkCategory Private" }
    }
}
elseif ($c -eq '2') {
    Push-Location $Root
    try { & $py -m analyzer lan off | Out-Null } finally { Pop-Location }
    if (Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue) {
        Write-Host '      방화벽 허용도 지웁니다. 권한 확인 창이 뜨면 [예]를 누르세요.' -ForegroundColor Cyan
        $null = Invoke-Admin "Remove-NetFirewallRule -DisplayName '$RuleName'"
    }
}
else { exit 0 }

Write-Host '      화면 서버를 다시 켭니다...'
Stop-Server
Start-Sleep -Milliseconds 800
$null = Start-Server $py
Write-Host ''
Push-Location $Root
try { & $py -m analyzer lan status } finally { Pop-Location }
Write-Host ''
if ($c -eq '1') {
    Write-Host '태블릿을 같은 와이파이에 연결한 뒤, 브라우저 주소창에 위 "태블릿에서 열 주소"를 넣으세요.' -ForegroundColor Green
    Write-Host '처음 한 번 비밀번호를 넣으면 30일 동안 다시 묻지 않습니다(PC를 다시 켜면 다시 물음).' -ForegroundColor Green
    Write-Host '윈도우 방화벽 알림 창이 뜨면 "개인 네트워크"만 체크하고 [허용]을 누르세요.' -ForegroundColor Green
}
Read-Host 'Enter를 누르면 닫힙니다'
