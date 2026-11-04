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

# 1-2) 데이터 폴더: 프로그램 폴더 밖(%LOCALAPPDATA%\CorpAnalyzer\data)에 둔다.
#      새 버전을 받아 프로그램 폴더를 바꿔도 모아 둔 DB가 그대로 남는다.
$settingsDir = Join-Path $env:APPDATA 'corp-analyzer'
$settingsFile = Join-Path $settingsDir 'settings.json'
$DataDir = Join-Path $env:LOCALAPPDATA 'CorpAnalyzer\data'
if (Test-Path $settingsFile) {
    try { $cur = (Get-Content $settingsFile -Raw | ConvertFrom-Json).data_dir; if ($cur) { $DataDir = $cur } } catch { }
}
if (-not (Test-Path (Join-Path $DataDir 'analyzer.db'))) {
    $src = Join-Path $Root 'data'
    $oldTask = Get-ScheduledTask -TaskName $TaskUpdate -ErrorAction SilentlyContinue
    if ($oldTask) {
        $oldRoot = $oldTask.Actions[0].WorkingDirectory
        if ($oldRoot -and ($oldRoot -ne $Root) -and (Test-Path (Join-Path $oldRoot 'data\analyzer.db'))) {
            $src = Join-Path $oldRoot 'data'
            Write-Host "      이전 설치 폴더($oldRoot)의 데이터를 옮깁니다." -ForegroundColor Yellow
        }
    }
    New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
    Copy-Item -Path (Join-Path $src '*') -Destination $DataDir -Recurse -Force
    Write-Host "      데이터 복사: $src → $DataDir"
}
New-Item -ItemType Directory -Force -Path $settingsDir | Out-Null
[IO.File]::WriteAllText($settingsFile, (@{ data_dir = $DataDir } | ConvertTo-Json), (New-Object System.Text.UTF8Encoding($false)))
Write-Host "      데이터 폴더: $DataDir" -ForegroundColor Green

# 2) DART 인증키 (저장 위치: %APPDATA%\corp-analyzer\dart_api_key.txt, 프로그램 폴더 밖)
$keyDir = Join-Path $env:APPDATA 'corp-analyzer'
$keyFile = Join-Path $keyDir 'dart_api_key.txt'
$old = if (Test-Path $keyFile) { "$(Get-Content $keyFile -Raw)".Trim() } else { '' }
$envKey = [Environment]::GetEnvironmentVariable('DART_API_KEY', 'User')
if (-not $envKey) { $envKey = [Environment]::GetEnvironmentVariable('DART_API_KEY', 'Machine') }
if ($envKey -and -not $old) { $old = $envKey.Trim(); Write-Host "[2/5] 환경변수 DART_API_KEY를 사용합니다(끝 4자리 ...$($old.Substring([Math]::Max(0,$old.Length-4))))." -ForegroundColor Green }
elseif ($old) { Write-Host "[2/5] 저장된 DART 인증키가 있습니다(끝 4자리 ...$($old.Substring([Math]::Max(0,$old.Length-4))))." }
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
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew `
    -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1)   # 서버가 꺼지면 1분 뒤 다시 시작(최대 5번)
$sAction = New-ScheduledTaskAction -Execute $pyw -Argument "-m analyzer serve --port $Port" -WorkingDirectory $Root
$sTrigger = New-ScheduledTaskTrigger -AtLogOn -User $me
Register-ScheduledTask -TaskName $TaskServer -Action $sAction -Trigger $sTrigger -Settings $sSettings -Principal $principal `
    -Description '상장기업 재무분석 화면 서버(이 PC에서만 접속 가능)' -Force | Out-Null
Write-Host "[4/5] 화면 서버 등록: 로그인할 때 자동 시작 ($TaskServer)" -ForegroundColor Green

# 바탕화면 바로가기
$desk = [Environment]::GetFolderPath('Desktop')
# '열기'는 서버가 꺼져 있으면 켜고 여는 .bat 으로 만든다(예전 .url 바로가기는 지움)
Remove-Item (Join-Path $desk '재무분석 열기.url') -ErrorAction SilentlyContinue
$utf8 = New-Object System.Text.UTF8Encoding($false)
$openTxt = (Get-Content (Join-Path $PSScriptRoot 'open.bat') -Raw).Replace('%~dp0', "$PSScriptRoot\")
[IO.File]::WriteAllText((Join-Path $desk '재무분석 열기.bat'), $openTxt, $utf8)
# 바탕화면 '지금 갱신' 파일(BOM 없는 UTF-8로 써야 cmd가 첫 줄을 읽음)
$batTxt = (Get-Content (Join-Path $PSScriptRoot 'run_now.bat') -Raw).Replace('%~dp0', "$PSScriptRoot\")
[IO.File]::WriteAllText((Join-Path $desk '재무분석 지금 갱신.bat'), $batTxt, (New-Object System.Text.UTF8Encoding($false)))

# 5) 첫 수집과 화면 열기
Write-Host '[5/5] 첫 수집을 실행합니다(빠진 값만 받습니다)...' -ForegroundColor Cyan
Push-Location $Root
try { & $py -m analyzer update-all } finally { Pop-Location }
Stop-Server            # 이전 버전 서버가 떠 있으면 끄고 새 코드로 다시 시작
$started = Start-Server $py
$v = Test-ServerVersion
if ($v.ok) { Write-Host "      화면 서버 버전 확인: $($v.running) (폴더 $($v.root))" -ForegroundColor Green }
else {
    Write-Host "      경고: 화면 서버 버전이 맞지 않습니다. 실행 중 $($v.running), 이 폴더 $(Get-LocalVersion)" -ForegroundColor Red
    Write-Host "      바탕화면 '재무분석 열기.bat'을 실행하면 원인을 진단해 보여 줍니다." -ForegroundColor Red
}
Start-Process $Url
Write-Host ''
Write-Host "설치 완료. 화면: $Url  (바탕화면 '재무분석 열기.bat')" -ForegroundColor Green
Write-Host "데이터·기록: $DataDir  ·  제거: windows\uninstall.bat"
