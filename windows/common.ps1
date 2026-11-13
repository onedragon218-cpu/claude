# 공통 함수: 저장소 경로, 파이썬 찾기, 작업 이름
$Root = Split-Path -Parent $PSScriptRoot          # windows\ 의 상위 폴더 = 프로그램 폴더
$TaskUpdate = 'CorpAnalyzer-Update'               # 평일 아침 자동 수집
$TaskServer = 'CorpAnalyzer-Server'               # 로그인할 때 화면 서버 시작
$Port = 8765
$Url = "http://localhost:$Port"

function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
                [Environment]::GetEnvironmentVariable('Path', 'User')
}

function Find-Python {
    # 3.10 이상인 python.exe 전체 경로를 돌려준다. 없으면 $null
    $cands = @()
    if (Get-Command py -ErrorAction SilentlyContinue) { $cands += ,@('py', '-3') }
    if (Get-Command python -ErrorAction SilentlyContinue) { $cands += ,@('python') }
    foreach ($c in $cands) {
        try {
            $exe = $c[0]; $pre = @(); if ($c.Count -gt 1) { $pre = $c[1..($c.Count - 1)] }
            $out = & $exe @pre -c "import sys;print(sys.executable);print(int(sys.version_info>=(3,10)))" 2>$null
            if ($LASTEXITCODE -eq 0 -and $out.Count -ge 2 -and $out[1].Trim() -eq '1' -and (Test-Path $out[0].Trim())) {
                return $out[0].Trim()
            }
        } catch { }
    }
    return $null
}

function Get-Pythonw([string]$python) {
    $w = Join-Path (Split-Path $python) 'pythonw.exe'
    if (Test-Path $w) { return $w } else { return $python }
}

function Test-Server {
    try {
        $c = New-Object System.Net.Sockets.TcpClient
        $iar = $c.BeginConnect('127.0.0.1', $Port, $null, $null)
        $ok = $iar.AsyncWaitHandle.WaitOne(500) -and $c.Connected
        $c.Close(); return $ok
    } catch { return $false }
}

function Start-Server([string]$python) {
    # 1) 작업 스케줄러 작업으로 켜 보고 2) 안 되면 파이썬을 직접 실행(창 없이)한다
    if (Test-Server) { return $true }
    if (Get-ScheduledTask -TaskName $TaskServer -ErrorAction SilentlyContinue) {
        Start-ScheduledTask -TaskName $TaskServer -ErrorAction SilentlyContinue
        for ($i = 0; $i -lt 25 -and -not (Test-Server); $i++) { Start-Sleep -Milliseconds 400 }
    }
    if (-not (Test-Server) -and $python) {
        Start-Process -FilePath (Get-Pythonw $python) -ArgumentList '-m', 'analyzer', 'serve', '--port', $Port `
            -WorkingDirectory $Root -WindowStyle Hidden
        for ($i = 0; $i -lt 40 -and -not (Test-Server); $i++) { Start-Sleep -Milliseconds 400 }
    }
    return (Test-Server)
}

function Stop-Server {
    # 새 버전 코드를 읽도록 실행 중인 화면 서버를 끈다(작업 스케줄러 작업, 명령줄, 8765 포트 사용 프로세스 모두)
    Stop-ScheduledTask -TaskName $TaskServer -ErrorAction SilentlyContinue
    Get-CimInstance Win32_Process -Filter "Name like 'python%'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like '*analyzer serve*' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    try {
        Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop |
            ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }
    } catch { }
    for ($i = 0; $i -lt 20 -and (Test-Server); $i++) { Start-Sleep -Milliseconds 300 }
}

function Get-LocalVersion {
    $f = Join-Path $Root 'analyzer\__init__.py'
    if (Test-Path $f) { if ((Get-Content $f -Raw -Encoding UTF8) -match '"([^"]+)"') { return $Matches[1] } }
    return '?'
}

function Test-ServerVersion {
    # 떠 있는 화면 서버가 이 폴더의 새 코드인지 확인
    try {
        $r = Invoke-RestMethod -Uri "$Url/api/version" -TimeoutSec 5
        return @{ ok = ($r.version -eq (Get-LocalVersion)); running = $r.version; root = $r.root }
    } catch { return @{ ok = $false; running = '(응답 없음)'; root = '' } }
}


# ---- 인증키: 화면·설치 기록(install_log.txt)에 남지 않게 받고, 이 PC 사용자만 읽게 저장 ----
$KeyDir = Join-Path $env:APPDATA 'corp-analyzer'
function Read-Secret([string]$prompt) {
    # -AsSecureString: 입력이 *로 보이고 설치 기록(Transcript)에도 남지 않는다. 붙여 넣기는 Ctrl+V 또는 마우스 오른쪽 클릭
    $sec = Read-Host $prompt -AsSecureString
    $b = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
    try { return "$([Runtime.InteropServices.Marshal]::PtrToStringBSTR($b))".Trim() } finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($b) }
}
function Save-Secret([string]$file, [string]$value) {
    New-Item -ItemType Directory -Force -Path (Split-Path $file) | Out-Null
    [IO.File]::WriteAllText($file, $value, (New-Object System.Text.ASCIIEncoding))
    Protect-Path $file
}
function Protect-Path([string]$p) {
    # 이 PC 사용자 + SYSTEM만 읽고 쓰게. 사용자 이름(한글 등) 대신 계정 고유번호(SID)로 지정하고 .NET으로 설정한다.
    # (예전 icacls 방식은 한글 사용자 이름이 깨져 '허용'은 빠지고 '차단'만 걸려 본인도 못 읽는 문제가 있었다 — 2026-11-13)
    if (-not (Test-Path $p)) { return }
    try {
        $me = [Security.Principal.WindowsIdentity]::GetCurrent().User
        $sys = New-Object Security.Principal.SecurityIdentifier('S-1-5-18')
        $dir = Test-Path $p -PathType Container
        $inh = if ($dir) { 'ContainerInherit,ObjectInherit' } else { 'None' }
        $item = Get-Item -LiteralPath $p -Force
        $acl = $item.GetAccessControl('Access')          # 접근 목록만 다룬다(파일 주인은 건드리지 않음)
        $acl.SetAccessRuleProtection($true, $false)
        foreach ($r in @($acl.GetAccessRules($true, $false, [Security.Principal.SecurityIdentifier]))) { [void]$acl.RemoveAccessRuleSpecific($r) }
        foreach ($id in @($me, $sys)) { $acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($id, 'FullControl', $inh, 'None', 'Allow'))) }
        $item.SetAccessControl($acl)
        if ($dir) { Get-ChildItem -LiteralPath $p -Force | ForEach-Object { Protect-Path $_.FullName } }
    } catch { Write-Host "      파일 권한 제한을 하지 못했습니다(파일은 그대로 쓸 수 있음): $p" -ForegroundColor Yellow }
}
function Repair-Secrets {
    # 설정·인증서·인증키 파일 권한을 '이 사용자 + SYSTEM'으로 다시 맞춘다(예전 방식으로 잠겨 못 읽던 파일도 풀림. 파일 주인은 권한을 바꿀 수 있음)
    foreach ($n in @('lan.json', 'tls', 'dart_api_key.txt', 'naver_api_key.txt', 'ecos_api_key.txt', 'exaone_api_key.txt')) { Protect-Path (Join-Path $KeyDir $n) }
}
function Clear-OldInstallLog {
    # 예전 버전 설치 기록에 인증키가 그대로 적혔을 수 있다 → 저장된 키가 기록에 보이면 기록 파일을 지운다(키 값은 화면에 내지 않음)
    $log = Join-Path $env:LOCALAPPDATA 'CorpAnalyzer\install_log.txt'
    if (-not (Test-Path $log)) { return }
    $txt = Get-Content $log -Raw -ErrorAction SilentlyContinue
    foreach ($n in @('dart_api_key.txt', 'naver_api_key.txt', 'ecos_api_key.txt', 'exaone_api_key.txt')) {
        $f = Join-Path $KeyDir $n
        if (-not (Test-Path $f)) { continue }
        foreach ($line in (Get-Content $f -ErrorAction SilentlyContinue)) {
            $v = "$line".Trim()
            if ($v.Length -ge 8 -and $txt -and $txt.Contains($v)) {
                Remove-Item $log -Force -ErrorAction SilentlyContinue
                Write-Host '      예전 설치 기록(install_log.txt)에 인증키가 적혀 있어 그 기록 파일을 지웠습니다.' -ForegroundColor Yellow
                return
            }
        }
    }
}
