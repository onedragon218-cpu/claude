# 제거: 작업 스케줄러 등록과 바로가기만 지운다. DB(data\analyzer.db)는 남긴다.
. (Join-Path $PSScriptRoot 'common.ps1')
foreach ($t in @($TaskUpdate, $TaskServer)) {
    if (Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $t -Confirm:$false
        Write-Host "삭제: $t"
    }
}
Get-CimInstance Win32_Process -Filter "Name like 'python%'" | Where-Object { $_.CommandLine -like '*analyzer serve*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force; Write-Host "화면 서버 종료(PID $($_.ProcessId))" }
$desk = [Environment]::GetFolderPath('Desktop')
Remove-Item (Join-Path $desk '재무분석 열기.url'), (Join-Path $desk '재무분석 지금 갱신.bat') -ErrorAction SilentlyContinue
$ans = Read-Host '저장된 DART 인증키도 지울까요? (y/N)'
if ($ans -eq 'y') { Remove-Item (Join-Path $env:APPDATA 'corp-analyzer') -Recurse -Force -ErrorAction SilentlyContinue; Write-Host '인증키 삭제' }
Write-Host "제거 완료. 데이터베이스는 $env:LOCALAPPDATA\CorpAnalyzer\data 에 그대로 남아 있습니다(지우려면 그 폴더를 직접 삭제)."
