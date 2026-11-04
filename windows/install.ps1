# 설치: 파이썬 확인 → DART 인증키 저장 → 작업 스케줄러 등록 → 첫 수집 → 화면 열기
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')
Write-Host ''
Write-Host '=== 상장기업 재무분석 시스템 설치 (Windows 11) ===' -ForegroundColor Cyan
Write-Host "프로그램 폴더: $Root"

# 0) 인터넷에서 받은 파일 차단 해제
Get-ChildItem $Root -Recurse -File | Unblock-File -ErrorAction SilentlyContinue

# 1) 파이썬
$py = Find-Python
if (-not $py) {
    Write-Host '[1/5] 파이썬 3.10 이상이 없습니다. winget으로 Python 3.12를 설치합니다(사용자 계정에만 설치).' -ForegroundColor Yellow
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Write-Host 'winget이 없습니다. https://www.python.org/downloads/ 에서 설치한 뒤 다시 실행하세요.' -ForegroundColor Red
        Write-Host '설치 화면에서 "Add python.exe to PATH"를 꼭 체크하세요.'
        exit 1
    }
    winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
    Refresh-Path
    $py = Find-Python
    if (-not $py) {
        $guess = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'
        if (Test-Path $guess) { $py = $guess }
    }
    if (-not $py) { Write-Host '파이썬 설치를 확인하지 못했습니다. 창을 닫고 install.bat을 다시 실행하세요.' -ForegroundColor Red; exit 1 }
}
Write-Host "[1/5] 파이썬: $py" -ForegroundColor Green
$pyw = Get-Pythonw $py

# 2) DART 인증키 (저장 위치: %APPDATA%\corp-analyzer\dart_api_key.txt, 프로그램 폴더 밖)
$keyDir = Join-Path $env:APPDATA 'corp-analyzer'
$keyFile = Join-Path $keyDir 'dart_api_key.txt'
$old = if (Test-Path $keyFile) { "$(Get-Content $keyFile -Raw)".Trim() } else { '' }
if ($old) { Write-Host "[2/5] 저장된 DART 인증키가 있습니다(끝 4자리 ...$($old.Substring([Math]::Max(0,$old.Length-4))))." }
$k = Read-Host '[2/5] DART 인증키 40자리를 붙여 넣으세요(바꾸지 않으려면 그냥 Enter, 나중에 넣으려면 Enter)'
$k = "$k".Trim()
if ($k) {
    try {
        $r = Invoke-RestMethod -Uri "https://opendart.fss.or.kr/api/company.json?crtfc_key=$k&corp_code=01060744" -TimeoutSec 20
        if ($r.status -eq '000') { Write-Host '      인증키 확인 완료' -ForegroundColor Green }
        else { Write-Host "      DART 응답: $($r.status) $($r.message) (키를 확인하세요. 일단 저장합니다)" -ForegroundColor Yellow }
    } catch { Write-Host "      DART 접속 확인 실패: $($_.Exception.Message) (일단 저장합니다)" -ForegroundColor Yellow }
    New-Item -ItemType Directory -Force -Path $keyDir | Out-Null
    Set-Content -Path $keyFile -Value $k -Encoding ASCII -NoNewline
    Write-Host "      저장: $keyFile"
} elseif (-not $old) {
    Write-Host '      인증키 없이 설치합니다. 이 경우 주식 수·새 공시는 수집되지 않습니다. 나중에 install.bat을 다시 실행해 넣으세요.' -ForegroundColor Yellow
}

# 3) 작업 스케줄러: 평일 07:13 자동 수집(꺼져 있던 날은 켜진 뒤 바로 실행)
$me = "$env:USERDOMAIN\$env:USERNAME"
$principal = New-ScheduledTaskPrincipal -UserId $me -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) -MultipleInstances IgnoreNew
$action = New-ScheduledTaskAction -Execute $pyw -Argument '-m analyzer update-all' -WorkingDirectory $Root
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At '07:13'
Register-ScheduledTask -TaskName $TaskUpdate -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
    -Description '상장기업 재무분석: DB에 없는 값·새 공시·컨센서스·기준일 주가만 수집' -Force | Out-Null
Write-Host "[3/5] 자동 수집 등록: 평일 07:13 ($TaskUpdate)" -ForegroundColor Green

# 4) 작업 스케줄러: 로그인하면 화면 서버 시작(http://localhost:8765)
$sSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
$sAction = New-ScheduledTaskAction -Execute $pyw -Argument "-m analyzer serve --port $Port" -WorkingDirectory $Root
$sTrigger = New-ScheduledTaskTrigger -AtLogOn -User $me
Register-ScheduledTask -TaskName $TaskServer -Action $sAction -Trigger $sTrigger -Settings $sSettings -Principal $principal `
    -Description '상장기업 재무분석 화면 서버(이 PC에서만 접속 가능)' -Force | Out-Null
Write-Host "[4/5] 화면 서버 등록: 로그인할 때 자동 시작 ($TaskServer)" -ForegroundColor Green

# 바탕화면 바로가기
$desk = [Environment]::GetFolderPath('Desktop')
Set-Content -Path (Join-Path $desk '재무분석 열기.url') -Value "[InternetShortcut]`r`nURL=$Url`r`n" -Encoding ASCII
# 바탕화면 '지금 갱신' 파일(BOM 없는 UTF-8로 써야 cmd가 첫 줄을 읽음)
$batTxt = (Get-Content (Join-Path $PSScriptRoot 'run_now.bat') -Raw).Replace('%~dp0', "$PSScriptRoot\")
[IO.File]::WriteAllText((Join-Path $desk '재무분석 지금 갱신.bat'), $batTxt, (New-Object System.Text.UTF8Encoding($false)))

# 5) 첫 수집과 화면 열기
Write-Host '[5/5] 첫 수집을 실행합니다(빠진 값만 받습니다)...' -ForegroundColor Cyan
Push-Location $Root
try { & $py -m analyzer update-all } finally { Pop-Location }
Start-Server $py
Start-Process $Url
Write-Host ''
Write-Host "설치 완료. 화면: $Url  (바탕화면 '재무분석 열기')" -ForegroundColor Green
Write-Host '기록: data\logs\  ·  제거: windows\uninstall.bat'
