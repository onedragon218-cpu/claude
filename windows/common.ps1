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
    if (Test-Server) { return }
    if (Get-ScheduledTask -TaskName $TaskServer -ErrorAction SilentlyContinue) {
        Start-ScheduledTask -TaskName $TaskServer
    } else {
        Start-Process -FilePath (Get-Pythonw $python) -ArgumentList '-m', 'analyzer', 'serve', '--port', $Port `
            -WorkingDirectory $Root -WindowStyle Hidden
    }
    for ($i = 0; $i -lt 20 -and -not (Test-Server); $i++) { Start-Sleep -Milliseconds 300 }
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
    if (Test-Path $f) { if ((Get-Content $f -Raw) -match '"([^"]+)"') { return $Matches[1] } }
    return '?'
}

function Test-ServerVersion {
    # 떠 있는 화면 서버가 이 폴더의 새 코드인지 확인
    try {
        $r = Invoke-RestMethod -Uri "$Url/api/version" -TimeoutSec 5
        return @{ ok = ($r.version -eq (Get-LocalVersion)); running = $r.version; root = $r.root }
    } catch { return @{ ok = $false; running = '(응답 없음)'; root = '' } }
}
