# 인증키 설정: DART·한국은행 ECOS·네이버 뉴스 키를 넣거나 바꾸거나 지운다.
# - 입력은 *로만 보이고, 화면·기록 파일에 키 값을 내지 않는다(저장 여부와 글자 수만 표시).
# - 저장 위치: %APPDATA%\corp-analyzer\ (프로그램 폴더 밖, 이 PC 사용자 계정만 읽기 가능)
# - 확인 요청은 이 PC에서 각 기관(DART·ECOS)으로 직접 보낸다. 다른 곳으로는 보내지 않는다.
$ErrorActionPreference = 'Continue'
. (Join-Path $PSScriptRoot 'common.ps1')
$Keys = [ordered]@{
    '1' = @{ name = 'DART 인증키(전자공시)';            file = 'dart_api_key.txt' }
    '2' = @{ name = '한국은행 ECOS 인증키(국고채 금리)'; file = 'ecos_api_key.txt' }
    '3' = @{ name = '네이버 뉴스 API(Client ID·Secret)'; file = 'naver_api_key.txt' }
}
function Show-State {
    foreach ($k in $Keys.Keys) {
        $f = Join-Path $KeyDir $Keys[$k].file
        $st = if (Test-Path $f) { $n = "$(Get-Content $f -Raw -ErrorAction SilentlyContinue)".Trim().Length; "저장됨($n자)" } else { '없음' }
        Write-Host ("  {0}) {1} : {2}" -f $k, $Keys[$k].name, $st)
    }
}
function Test-Ecos([string]$v) {
    try {
        $r = Invoke-RestMethod -Uri "https://ecos.bok.or.kr/api/StatisticSearch/$v/json/kr/1/1/721Y001/A/2024/2024/5050000" -TimeoutSec 20
        if ($r.StatisticSearch.row) { return '확인 완료(ECOS 응답 정상)' }
        if ($r.RESULT) { return "ECOS 응답: $($r.RESULT.CODE) $($r.RESULT.MESSAGE)" }
        return 'ECOS 응답을 해석하지 못했습니다'
    } catch { return 'ECOS 접속 확인 실패(인터넷 연결 확인). 일단 저장합니다' }   # 오류 문구에 주소(키 포함)가 섞일 수 있어 내지 않음
}
function Test-Dart([string]$v) {
    try {
        $r = Invoke-RestMethod -Uri "https://opendart.fss.or.kr/api/company.json?crtfc_key=$v&corp_code=01060744" -TimeoutSec 20
        if ($r.status -eq '000') { return '확인 완료(DART 응답 정상)' } else { return "DART 응답: $($r.status) $($r.message)" }
    } catch { return 'DART 접속 확인 실패(인터넷 연결 확인). 일단 저장합니다' }
}

Write-Host '=== 재무분석 인증키 설정 ===' -ForegroundColor Cyan
Write-Host "  저장 위치: $KeyDir (이 PC 사용자 계정만 읽기 가능)"
Clear-OldInstallLog
while ($true) {
    Write-Host ''
    Show-State
    Write-Host '  고르기: 1~3 = 넣기·바꾸기, d1~d3 = 지우기, Enter = 닫기'
    $c = "$(Read-Host '번호')".Trim()
    if (-not $c) { break }
    $del = $c.StartsWith('d'); $id = $c.TrimStart('d')
    if (-not $Keys.Contains($id)) { Write-Host '  잘못 고르셨습니다.' -ForegroundColor Yellow; continue }
    $f = Join-Path $KeyDir $Keys[$id].file
    if ($del) {
        if ((Read-Host "  $($Keys[$id].name)를 지울까요? (y/N)") -eq 'y') { Remove-Item $f -Force -ErrorAction SilentlyContinue; Write-Host '  지웠습니다.' -ForegroundColor Green }
        continue
    }
    if ($id -eq '3') {
        $a = Read-Secret '  Client ID (입력은 *로 보임)'; if (-not $a) { continue }
        $b = Read-Secret '  Client Secret (입력은 *로 보임)'; if (-not $b) { continue }
        Save-Secret $f "$a`r`n$b"; $a = $null; $b = $null
        Write-Host '  저장했습니다.' -ForegroundColor Green; continue
    }
    $v = Read-Secret "  $($Keys[$id].name)를 붙여 넣으세요(Ctrl+V 또는 마우스 오른쪽 클릭, 입력은 *로 보임)"
    if (-not $v) { Write-Host '  비어 있어 바꾸지 않았습니다.'; continue }
    if ($v -match '\s') { Write-Host '  키 중간에 빈칸이 있습니다. 다시 복사해 넣어 주세요.' -ForegroundColor Yellow; continue }
    $msg = if ($id -eq '1') { Test-Dart $v } else { Test-Ecos $v }
    Save-Secret $f $v; $v = $null
    Write-Host "  저장했습니다 · $msg" -ForegroundColor Green
}
Write-Host '  바뀐 키는 다음 분석·갱신부터 쓰입니다(화면 서버를 다시 켤 필요 없음).'
