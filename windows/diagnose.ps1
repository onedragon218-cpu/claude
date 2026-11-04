# 진단: 설치 상태, 작업 스케줄러, 포트, 최근 기록을 보여 주고 바탕화면에 '재무분석_진단.txt'로 저장한다.
. (Join-Path $PSScriptRoot 'common.ps1')
$out = Join-Path ([Environment]::GetFolderPath('Desktop')) '재무분석_진단.txt'
& {
    "== 진단 시각: $(Get-Date)"
    "프로그램 폴더: $Root  · 이 폴더의 버전: $(Get-LocalVersion)"
    $py = Find-Python
    "파이썬: $py"
    foreach ($t in @($TaskUpdate, $TaskServer)) {
        $task = Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue
        if ($task) {
            $info = Get-ScheduledTaskInfo -TaskName $t
            "작업 $t : 상태 $($task.State), 마지막 실행 $($info.LastRunTime), 결과 코드 $($info.LastTaskResult), 실행 파일 $($task.Actions[0].Execute) $($task.Actions[0].Arguments), 폴더 $($task.Actions[0].WorkingDirectory)"
        } else { "작업 $t : 없음(install.bat 필요)" }
    }
    "포트 $Port 응답: $(Test-Server)"
    try { Get-NetTCPConnection -LocalPort $Port -ErrorAction Stop | ForEach-Object { "  포트 사용: PID $($_.OwningProcess) 상태 $($_.State)" } } catch { "  포트 사용 프로세스 없음" }
    Get-CimInstance Win32_Process -Filter "Name like 'python%'" -ErrorAction SilentlyContinue |
        ForEach-Object { "  파이썬 프로세스 PID $($_.ProcessId): $($_.CommandLine)" }
    if ($py) { Push-Location $Root; try { & $py -m analyzer doctor 2>&1 } finally { Pop-Location } }
} | Tee-Object -FilePath $out
Write-Host ''
Write-Host "진단 결과를 바탕화면 '재무분석_진단.txt'에 저장했습니다. 이 파일 내용을 보내 주세요." -ForegroundColor Green
Read-Host 'Enter를 누르면 닫힙니다'
