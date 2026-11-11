# 태블릿 접속 설정: 같은 와이파이의 태블릿·휴대폰에서 화면을 https + 비밀번호로 열 수 있게 켜거나 끈다.
. (Join-Path $PSScriptRoot 'common.ps1')
$py = Find-Python
if (-not $py) { Write-Host '파이썬을 찾지 못했습니다. install.bat을 먼저 실행하세요.' -ForegroundColor Red; Read-Host 'Enter를 누르면 닫힙니다'; exit 1 }
$HttpsPort = 8766
$RuleName = 'CorpAnalyzer 태블릿 접속(https)'
$OldRule = 'CorpAnalyzer 태블릿 접속'          # 이전 버전(암호화 없는 8765)이 만든 허용 — 있으면 지운다
$CfgDir = Join-Path $env:APPDATA 'corp-analyzer'
$OutputEncoding = New-Object System.Text.UTF8Encoding($false)   # 파이썬으로 넘기는 비밀번호가 깨지지 않게
$env:PYTHONIOENCODING = 'utf-8'

function Desktop-Dirs {
    # 바탕화면 후보(원드라이브로 옮겨진 PC 포함). 없는 경로는 빼고 겹치면 하나만
    $c = @([Environment]::GetFolderPath('Desktop'), (Join-Path $env:USERPROFILE 'Desktop'))
    if ($env:OneDrive) { $c += (Join-Path $env:OneDrive 'Desktop'); $c += (Join-Path $env:OneDrive '바탕 화면') }
    return @($c | Where-Object { $_ -and (Test-Path $_) } | Select-Object -Unique)
}

function Invoke-Admin([string]$cmd) {
    # 관리자 권한이 필요한 명령(방화벽·네트워크 종류)만 따로 권한을 요청해 실행
    try { Start-Process powershell -Verb RunAs -Wait -WindowStyle Hidden -ArgumentList '-NoProfile', '-Command', $cmd; return $true }
    catch { Write-Host "      관리자 권한을 받지 못했습니다: $($_.Exception.Message)" -ForegroundColor Yellow; return $false }
}
function Py-Lan([string[]]$a, [string]$stdin) {
    # 파이썬 출력은 화면에 바로 보이고(Out-Host), 함수는 종료 코드만 돌려준다
    Push-Location $Root
    try {
        if ($stdin) { $stdin | & $py -m analyzer lan @a 2>&1 | Out-Host } else { & $py -m analyzer lan @a 2>&1 | Out-Host }
        return $LASTEXITCODE
    } finally { Pop-Location }
}
function Plain($sec) {
    $b = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($b) } finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($b) }
}
function Lock-Files {
    # 설정·인증서 키 파일을 이 PC 사용자만 읽게(다른 사용자 계정·프로그램이 못 읽도록)
    $f = Join-Path $CfgDir 'lan.json'
    if (Test-Path $f) { & icacls $f /inheritance:r /grant:r "$($env:USERNAME):F" /grant:r 'SYSTEM:F' /Q | Out-Null }
    $d = Join-Path $CfgDir 'tls'
    if (Test-Path $d) { & icacls $d /inheritance:r /grant:r "$($env:USERNAME):(OI)(CI)F" /grant:r 'SYSTEM:(OI)(CI)F' /T /Q | Out-Null }
}
function Ensure-DesktopShortcut {
    $txt = (Get-Content (Join-Path $PSScriptRoot 'lan.bat') -Raw).Replace('%~dp0', "$PSScriptRoot\")
    foreach ($d in (Desktop-Dirs)) {
        $f = Join-Path $d '재무분석 태블릿 접속 설정.bat'
        if (-not (Test-Path $f)) { [IO.File]::WriteAllText($f, $txt, (New-Object System.Text.UTF8Encoding($false))); Write-Host "      바탕화면 바로가기 만듦: $f" }
    }
}

Write-Host '=== 재무분석 태블릿 접속 설정 ===' -ForegroundColor Cyan
$null = Py-Lan @('status')
Write-Host ''
Write-Host '1) 켜기 / 비밀번호 새로 정하기   2) 끄기   3) 인증서 새로 만들기   4) 그대로 두고 닫기'
$c = Read-Host '번호를 고르세요'

if ($c -eq '1') {
    $pw = $null
    for ($i = 0; $i -lt 3 -and -not $pw; $i++) {
        Write-Host '      비밀번호: 8자 이상, 영문·숫자·기호 중 두 가지 이상. 다른 곳에서 쓰는 비밀번호는 피하세요.'
        $t1 = Plain (Read-Host '      태블릿 접속 비밀번호' -AsSecureString)
        $t2 = Plain (Read-Host '      한 번 더' -AsSecureString)
        if ($t1 -ne $t2) { Write-Host '      두 번 넣은 비밀번호가 다릅니다.' -ForegroundColor Red; continue }
        if ((Py-Lan @('check', '--password-stdin') $t1) -ne 0) { continue }
        $pw = $t1
    }
    if (-not $pw) { Write-Host '비밀번호를 정하지 못했습니다. 다시 실행하세요.' -ForegroundColor Red; Read-Host 'Enter를 누르면 닫힙니다'; exit 1 }
    $w = Read-Host '      태블릿에서 수집·갱신·저장도 할 수 있게 할까요? 보기만 하려면 Enter (y/N)'
    $args2 = @('on', '--password-stdin'); if ($w -eq 'y') { $args2 += '--allow-write' }
    $null = Py-Lan $args2 $pw
    $pw = $null; $t1 = $null; $t2 = $null
    Lock-Files

    # 방화벽: 이전 버전의 8765 허용은 지우고, 8766(https)만 '개인 네트워크 + 같은 공유기 안(LocalSubnet)'에서 허용
    $need = (-not (Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue)) -or (Get-NetFirewallRule -DisplayName $OldRule -ErrorAction SilentlyContinue)
    if ($need) {
        Write-Host '      방화벽을 설정합니다. 권한 확인 창이 뜨면 [예]를 누르세요.' -ForegroundColor Cyan
        $null = Invoke-Admin "Remove-NetFirewallRule -DisplayName '$OldRule' -ErrorAction SilentlyContinue; if (-not (Get-NetFirewallRule -DisplayName '$RuleName' -ErrorAction SilentlyContinue)) { New-NetFirewallRule -DisplayName '$RuleName' -Direction Inbound -Protocol TCP -LocalPort $HttpsPort -RemoteAddress LocalSubnet -Profile Private -Action Allow }"
    }
    if (Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue) { Write-Host "      방화벽: $HttpsPort 포트를 같은 공유기 안(개인 네트워크)에서만 허용" -ForegroundColor Green }
    else { Write-Host '      방화벽 설정을 하지 못했습니다. 태블릿에서 열리지 않으면 이 파일을 다시 실행해 [예]를 눌러 주세요.' -ForegroundColor Yellow }
    if (Get-NetFirewallRule -DisplayName $OldRule -ErrorAction SilentlyContinue) { Write-Host '      주의: 이전 버전의 8765 허용이 남아 있습니다(권한 창에서 [예]를 눌러야 지워짐).' -ForegroundColor Yellow }

    # 와이파이가 '공용' 네트워크면 허용이 적용되지 않음 → 집 와이파이라면 '개인'으로 바꿀지 묻기
    $pub = Get-NetConnectionProfile -ErrorAction SilentlyContinue | Where-Object { $_.NetworkCategory -eq 'Public' }
    foreach ($n in $pub) {
        $a = Read-Host "      네트워크 '$($n.Name)'가 '공용'입니다. 집 와이파이라면 '개인'으로 바꿀까요? 카페·회사 등 공용 와이파이면 n (y/N)"
        if ($a -eq 'y') { $null = Invoke-Admin "Set-NetConnectionProfile -InterfaceIndex $($n.InterfaceIndex) -NetworkCategory Private" }
    }
}
elseif ($c -eq '2') {
    $null = Py-Lan @('off')
    if ((Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue) -or (Get-NetFirewallRule -DisplayName $OldRule -ErrorAction SilentlyContinue)) {
        Write-Host '      방화벽 허용도 지웁니다. 권한 확인 창이 뜨면 [예]를 누르세요.' -ForegroundColor Cyan
        $null = Invoke-Admin "Remove-NetFirewallRule -DisplayName '$RuleName' -ErrorAction SilentlyContinue; Remove-NetFirewallRule -DisplayName '$OldRule' -ErrorAction SilentlyContinue"
    }
}
elseif ($c -eq '3') { $null = Py-Lan @('renew-cert'); Lock-Files }
else { Ensure-DesktopShortcut; exit 0 }

Ensure-DesktopShortcut
Write-Host '      화면 서버를 다시 켭니다...'
Stop-Server
Start-Sleep -Milliseconds 800
$null = Start-Server $py
Write-Host ''
$null = Py-Lan @('status')
Write-Host ''
if ($c -eq '1' -or $c -eq '3') {
    Write-Host '태블릿을 같은 와이파이에 연결한 뒤, 브라우저 주소창에 위 "태블릿에서 열 주소"(https://…:8766)를 넣으세요.' -ForegroundColor Green
    Write-Host '처음 한 번 "연결이 비공개가 아님/안전하지 않음" 경고가 나옵니다. 이 PC가 직접 만든 인증서라서 그렇습니다.' -ForegroundColor Green
    Write-Host '  → 브라우저의 인증서 정보에서 지문(SHA-256)이 위 "인증서 지문"과 같은지 확인한 뒤 [고급] → [계속/방문]을 누르세요.' -ForegroundColor Green
    Write-Host '로그인은 7일 동안 유지됩니다(PC를 다시 켜면 다시 로그인). 접속 기록은 이 창을 다시 열면 보입니다.' -ForegroundColor Green
}
Read-Host 'Enter를 누르면 닫힙니다'
