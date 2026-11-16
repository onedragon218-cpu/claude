# 설치: 파이썬 확인 → DART 인증키 저장 → 작업 스케줄러 등록 → 화면 서버 재시작 → 바로가기 → 첫 수집(화면 뒤) → 화면 열기
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')
# 설치 기록: 화면에 나온 글을 그대로 %LOCALAPPDATA%\CorpAnalyzer\install_log.txt 에 남긴다(문제가 생기면 이 파일을 보내 주세요)
$InstallLog = Join-Path $env:LOCALAPPDATA 'CorpAnalyzer\install_log.txt'
try { New-Item -ItemType Directory -Force -Path (Split-Path $InstallLog) | Out-Null; Start-Transcript -Path $InstallLog -Force | Out-Null } catch { }
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
    # settings.json은 UTF-8로 저장되어 있다. PowerShell 5.1은 인코딩을 안 주면 한국어 윈도우 기본(CP949)으로 읽어
    # 한글 사용자 이름(예: C:\Users\김희진)이 깨지므로 반드시 UTF-8로 읽는다.
    try {
        $cur = (Get-Content $settingsFile -Raw -Encoding UTF8 | ConvertFrom-Json).data_dir
        if ($cur -and ($cur.IndexOfAny([IO.Path]::GetInvalidPathChars()) -lt 0) -and ($cur -notmatch '\?') -and (Test-Path (Split-Path $cur -Qualifier))) { $DataDir = $cur }
        elseif ($cur) { Write-Host "      설정 파일의 데이터 폴더 경로가 올바르지 않아 기본 위치를 씁니다: $cur" -ForegroundColor Yellow }
    } catch { }
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
    try { New-Item -ItemType Directory -Force -Path $DataDir | Out-Null }
    catch { Write-Host "      데이터 폴더를 만들지 못했습니다: $DataDir ($($_.Exception.Message))" -ForegroundColor Red; throw }
    Copy-Item -Path (Join-Path $src '*') -Destination $DataDir -Recurse -Force
    Write-Host "      데이터 복사: $src → $DataDir"
}
New-Item -ItemType Directory -Force -Path $settingsDir | Out-Null
[IO.File]::WriteAllText($settingsFile, (@{ data_dir = $DataDir } | ConvertTo-Json), (New-Object System.Text.UTF8Encoding($false)))
Write-Host "      데이터 폴더: $DataDir" -ForegroundColor Green

# 2) DART 인증키 (저장 위치: %APPDATA%\corp-analyzer\dart_api_key.txt, 프로그램 폴더 밖)
$keyDir = Join-Path $env:APPDATA 'corp-analyzer'
$keyFile = Join-Path $keyDir 'dart_api_key.txt'
$old = if (Test-Path $keyFile) { "$(Get-Content $keyFile -Raw -Encoding UTF8)".Trim() } else { '' }
$envKey = [Environment]::GetEnvironmentVariable('DART_API_KEY', 'User')
if (-not $envKey) { $envKey = [Environment]::GetEnvironmentVariable('DART_API_KEY', 'Machine') }
if ($envKey -and -not $old) { $old = $envKey.Trim(); Write-Host "[2/5] 환경변수 DART_API_KEY를 사용합니다(끝 4자리 ...$($old.Substring([Math]::Max(0,$old.Length-4))))." -ForegroundColor Green }
elseif ($old) { Write-Host "[2/5] 저장된 DART 인증키가 있습니다(끝 4자리 ...$($old.Substring([Math]::Max(0,$old.Length-4))))." }
$k = Read-Secret '[2/5] DART 인증키 40자리를 붙여 넣으세요(입력은 *로 보임. 바꾸지 않으려면 그냥 Enter)'
if ($k) {
    try {
        $r = Invoke-RestMethod -Uri "https://opendart.fss.or.kr/api/company.json?crtfc_key=$k&corp_code=01060744" -TimeoutSec 20
        if ($r.status -eq '000') { Write-Host '      인증키 확인 완료' -ForegroundColor Green }
        else { Write-Host "      DART 응답: $($r.status) $($r.message) (키를 확인하세요. 일단 저장합니다)" -ForegroundColor Yellow }
    } catch { Write-Host "      DART 접속 확인 실패: $($_.Exception.Message) (일단 저장합니다)" -ForegroundColor Yellow }
    Save-Secret $keyFile $k
    Write-Host "      저장: $keyFile"
} elseif (-not $old) {
    Write-Host '      인증키 없이 설치합니다. 이 경우 주식 수·새 공시는 수집되지 않습니다. 나중에 install.bat을 다시 실행해 넣으세요.' -ForegroundColor Yellow
}

# 2-1) 네이버 뉴스 검색 API 키(선택). 없으면 구글 뉴스 RSS로 뉴스 제목을 받는다.
#      저장 위치: %APPDATA%\corp-analyzer\naver_api_key.txt (1줄 Client ID, 2줄 Client Secret)
$nvFile = Join-Path $keyDir 'naver_api_key.txt'
if (Test-Path $nvFile) { Write-Host '      저장된 네이버 뉴스 API 키가 있습니다.' }
$nid = Read-Secret '[2/5] (선택) 네이버 뉴스 API Client ID (입력은 *로 보임, 없으면 그냥 Enter)'
if ($nid) {
    $nsec = Read-Secret '      네이버 뉴스 API Client Secret'
    if ($nsec) {
        Save-Secret $nvFile "$nid`r`n$nsec"
        Write-Host "      저장: $nvFile" -ForegroundColor Green
    }
}

# 2-2) 한국은행 ECOS 인증키(선택). 있으면 국고채 10년 금리(할인율의 바탕)를 1주일마다 자동으로 받는다.
#      저장 위치: %APPDATA%\corp-analyzer\ecos_api_key.txt  · 발급: ecos.bok.or.kr → 인증키 신청(무료)
$ecFile = Join-Path $keyDir 'ecos_api_key.txt'
if (Test-Path $ecFile) { Write-Host '      저장된 한국은행 ECOS 인증키가 있습니다.' }
$ek = Read-Secret '      (선택) 한국은행 ECOS 인증키 - 국고채 금리 자동 갱신용 (입력은 *로 보임, 없으면 그냥 Enter)'
if ($ek) {
    Save-Secret $ecFile $ek
    Write-Host "      저장: $ecFile" -ForegroundColor Green
}

Repair-Secrets      # 인증키·태블릿 인증서 파일 권한 바로잡기(예전 버전에서 잠겨 못 읽던 경우)
# 예전 설치 기록은 이번 설치가 시작될 때 새 기록으로 덮어써졌다(키 입력은 이제 기록에 남지 않음)

# 3) 작업 스케줄러: 평일 07:13 자동 수집(꺼져 있던 날은 켜진 뒤 바로 실행)
$me = "$env:USERDOMAIN\$env:USERNAME"
$principal = New-ScheduledTaskPrincipal -UserId $me -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 6) -MultipleInstances IgnoreNew   # 기업이 많으면 오래 걸림(끊겨도 다음에 이어서 받음)
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
    -Description '상장기업 재무분석 화면 서버(이 PC, 또는 태블릿 접속을 켠 경우 같은 와이파이 기기)' -Force | Out-Null
Write-Host "[4/5] 화면 서버 등록: 로그인할 때 자동 시작 ($TaskServer)" -ForegroundColor Green

# 바탕화면 바로가기
$desk = [Environment]::GetFolderPath('Desktop')
# '열기'는 서버가 꺼져 있으면 켜고 여는 .bat 으로 만든다(예전 .url 바로가기는 지움)
Remove-Item (Join-Path $desk '재무분석 열기.url') -ErrorAction SilentlyContinue
$utf8 = New-Object System.Text.UTF8Encoding($false)
$openTxt = (Get-Content (Join-Path $PSScriptRoot 'open.bat') -Raw -Encoding UTF8).Replace('%~dp0', "$PSScriptRoot\")
[IO.File]::WriteAllText((Join-Path $desk '재무분석 열기.bat'), $openTxt, $utf8)
# 바탕화면 '지금 갱신' 파일(BOM 없는 UTF-8로 써야 cmd가 첫 줄을 읽음)
$batTxt = (Get-Content (Join-Path $PSScriptRoot 'run_now.bat') -Raw -Encoding UTF8).Replace('%~dp0', "$PSScriptRoot\")
[IO.File]::WriteAllText((Join-Path $desk '재무분석 지금 갱신.bat'), $batTxt, (New-Object System.Text.UTF8Encoding($false)))

# 5) 화면 서버를 새 코드로 다시 켜기(수집보다 먼저 — 설치 창을 일찍 닫아도 새 버전이 뜨도록)
Write-Host '[5/5] 화면 서버를 새 버전으로 다시 켭니다...' -ForegroundColor Cyan
Stop-Server            # 이전 버전 서버가 떠 있으면 끄고 새 코드로 다시 시작
$started = Start-Server $py
$v = Test-ServerVersion
if ($v.ok) { Write-Host "      화면 서버 버전 확인: $($v.running) (폴더 $($v.root))" -ForegroundColor Green }
else {
    Write-Host "      경고: 화면 서버 버전이 맞지 않습니다. 실행 중 $($v.running), 이 폴더 $(Get-LocalVersion)" -ForegroundColor Red
    Write-Host "      바탕화면 '재무분석 열기.bat'을 실행하면 원인을 진단해 보여 줍니다." -ForegroundColor Red
}

# 바로가기 확인과 '태블릿 접속 설정' 바로가기: 맨 마지막에 만들고 결과를 요약해 보여 준다
function New-Lnk([string]$path, [string]$target, [string]$desc) {
    $sh = New-Object -ComObject WScript.Shell
    $l = $sh.CreateShortcut($path)
    $l.TargetPath = $target
    $l.WorkingDirectory = Split-Path $target
    $l.Description = $desc
    $l.Save()
}
Write-Host ''
Write-Host '=== 바로가기 ===' -ForegroundColor Cyan
$lanBat = Join-Path $PSScriptRoot 'lan.bat'
$cand = @($desk, (Join-Path $env:USERPROFILE 'Desktop'))
if ($env:OneDrive) { $cand += (Join-Path $env:OneDrive 'Desktop'); $cand += (Join-Path $env:OneDrive '바탕 화면') }
$desks = @($cand | Where-Object { $_ -and (Test-Path $_) } | Select-Object -Unique)
$okLan = $false
foreach ($d in $desks) {
    $f = Join-Path $d '재무분석 태블릿 접속 설정.lnk'
    try {
        Remove-Item (Join-Path $d '재무분석 태블릿 접속 설정.bat') -ErrorAction SilentlyContinue   # 예전 방식(.bat 복사본)은 지움
        New-Lnk $f $lanBat '같은 와이파이 태블릿에서 화면 보기(https + 비밀번호) 켜기·끄기'
        if (Test-Path $f) { Write-Host "  [만듦] $f" -ForegroundColor Green; $okLan = $true }
        else { Write-Host "  [실패] $f (만든 뒤 파일이 없음 — 백신 프로그램이 지웠을 수 있음)" -ForegroundColor Yellow }
    } catch { Write-Host "  [실패] $f : $($_.Exception.Message)" -ForegroundColor Yellow }
}
foreach ($d in $desks) {      # 인증키 넣기·바꾸기 바로가기(바탕화면)
    $f = Join-Path $d '재무분석 인증키 설정.lnk'
    try {
        New-Lnk $f (Join-Path $PSScriptRoot 'keys.bat') 'DART·ECOS·네이버 인증키 넣기·바꾸기(입력은 *로 보임)'
        if (Test-Path $f) { Write-Host "  [만듦] $f" -ForegroundColor Green }
    } catch { Write-Host "  [실패] $f : $($_.Exception.Message)" -ForegroundColor Yellow }
}
try {
    $sm = Join-Path ([Environment]::GetFolderPath('Programs')) '재무분석'
    New-Item -ItemType Directory -Force -Path $sm | Out-Null
    New-Lnk (Join-Path $sm '재무분석 태블릿 접속 설정.lnk') $lanBat '같은 와이파이 태블릿에서 화면 보기 켜기·끄기'
    New-Lnk (Join-Path $sm '재무분석 열기.lnk') (Join-Path $PSScriptRoot 'open.bat') '재무분석 화면 열기'
    New-Lnk (Join-Path $sm '재무분석 인증키 설정.lnk') (Join-Path $PSScriptRoot 'keys.bat') 'DART·ECOS·네이버 인증키 넣기·바꾸기(입력은 *로 보임)'
    Write-Host "  [만듦] 시작 메뉴 → 재무분석 폴더" -ForegroundColor Green
} catch { Write-Host "  [실패] 시작 메뉴: $($_.Exception.Message)" -ForegroundColor Yellow }
foreach ($n in @('재무분석 열기.bat', '재무분석 지금 갱신.bat')) {
    $f = Join-Path $desk $n
    if (Test-Path $f) { Write-Host "  [있음] $f" } else { Write-Host "  [없음] $f" -ForegroundColor Yellow }
}
if (-not $okLan) {
    Write-Host "  바탕화면에 '태블릿 접속 설정'을 만들지 못했습니다. 아래 파일을 직접 실행하세요:" -ForegroundColor Yellow
    Write-Host "    $lanBat" -ForegroundColor Yellow
}

# 첫 수집: 설치 창에서 기다리지 않고 화면 뒤에서 돌린다(자동 수집과 같은 작업). 도는 동안에도 화면을 그대로 쓸 수 있다.
Write-Host ''
Write-Host '=== 첫 수집(빠진 값만) ===' -ForegroundColor Cyan
$bgOk = $false
try { Start-ScheduledTask -TaskName $TaskUpdate -ErrorAction Stop; $bgOk = $true } catch { }
if (-not $bgOk) {
    try { Start-Process -FilePath $pyw -ArgumentList '-m analyzer update-all' -WorkingDirectory $Root -WindowStyle Hidden; $bgOk = $true } catch { }
}
$runLog = Join-Path $DataDir ("logs\run_{0:yyyyMMdd}.log" -f (Get-Date))
if ($bgOk) {
    Write-Host '  화면 뒤에서 수집을 시작했습니다. 기다리지 않고 바로 화면을 쓰셔도 됩니다.' -ForegroundColor Green
    Write-Host '  기업이 많으면 몇십 분 걸릴 수 있고, 끝난 기업부터 화면에 반영됩니다(새로 고침).'
    Write-Host "  진행 기록: $runLog"
} else {
    Write-Host "  화면 뒤 수집을 시작하지 못했습니다. 바탕화면 '재무분석 지금 갱신.bat'을 실행하세요." -ForegroundColor Yellow
}

Start-Process $Url
Write-Host ''
Write-Host "설치 완료. 화면: $Url  (바탕화면 '재무분석 열기.bat')" -ForegroundColor Green
Write-Host "데이터·기록: $DataDir  ·  제거: windows\uninstall.bat"
Write-Host "설치 기록: $InstallLog"
try { Stop-Transcript | Out-Null } catch { }
