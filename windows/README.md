# 윈도우11 PC에서 자동 실행하기

설치하면 PC에서 아래 두 가지가 자동으로 돌아갑니다.

| 작업(작업 스케줄러 이름) | 언제 | 하는 일 |
|---|---|---|
| `CorpAnalyzer-Update` | 평일 07:13. PC가 꺼져 있었으면 켜진 직후 | DB에 없는 값, 새 공시·정정 공시, 컨센서스(7일마다), 기준일 주가만 수집하고 화면을 다시 만듭니다 |
| `CorpAnalyzer-Server` | 윈도우에 로그인할 때 | 화면 서버를 시작합니다. 주소는 http://localhost:8765 이고, 이 PC에서만 열립니다 |

## 받는 곳

- 저장소: https://github.com/onedragon218-cpu/claude (브랜치 `claude/zen-lamport-1jplcv`)
- ZIP 바로 받기(GitHub 로그인 상태에서): https://github.com/onedragon218-cpu/claude/archive/refs/heads/claude/zen-lamport-1jplcv.zip

## 처음 설치

1. 위 ZIP을 받아 **`C:\CorpAnalyzer`** 에 압축을 풉니다. 폴더 안에 `analyzer`, `windows`, `data` 폴더가 보여야 합니다. ZIP을 풀면 폴더가 한 겹 더 생길 수 있으니, 그 안의 내용을 `C:\CorpAnalyzer`로 옮기십시오.
2. **`C:\CorpAnalyzer\windows\install.bat`** 을 더블클릭합니다.
   - "Windows의 PC 보호" 창이 뜨면 → 추가 정보 → 실행을 누릅니다.
   - 파이썬은 이미 설치되어 있으면 그대로 씁니다. 없으면 winget으로 자동 설치합니다.
   - DART 인증키: 사용자 환경변수 `DART_API_KEY`에 이미 있으면 그냥 Enter를 누릅니다. 없으면 붙여 넣습니다.
   - 데이터 폴더는 `%LOCALAPPDATA%\CorpAnalyzer\data` 에 만들어집니다. 프로그램 폴더 밖이라 새 버전으로 바꿔도 DB가 남습니다.
   - 첫 수집이 끝나면 브라우저가 열립니다.
3. 바탕화면에 `재무분석 열기.bat`과 `재무분석 지금 갱신.bat`이 생깁니다.

## 새 버전으로 바꾸기(이미 설치한 경우)

**데이터 폴더를 분리한 버전(가치평가 화면 G가 있는 버전)부터**는 DB가 `%LOCALAPPDATA%\CorpAnalyzer\data`에 있습니다.

1. 새 ZIP의 내용을 **기존 폴더(`C:\CorpAnalyzer`)에 덮어씁니다.** "대상 폴더에 같은 이름의 파일이 있습니다" 창이 뜨면 "파일 바꾸기"를 누릅니다.
2. `C:\CorpAnalyzer\windows\install.bat`을 다시 실행합니다. 인증키를 물으면 Enter만 누릅니다. 실행 중인 화면 서버를 끄고 새 코드로 다시 켭니다.
3. 브라우저에서 새 기능이 보이면 끝입니다. 보이지 않으면 Ctrl+F5로 새로고침합니다.

그보다 오래된 버전(화면 G가 없는 버전)이라면, 새 ZIP을 **새 폴더**에 풀고 그 폴더의 `install.bat`을 실행하십시오. 기존 DB를 자동으로 옮깁니다.

## 자주 쓰는 동작

| 하고 싶은 일 | 방법 |
|---|---|
| 화면 보기 | 바탕화면 `재무분석 열기.bat` 또는 `windows\open.bat`. 서버가 꺼져 있으면 켜고 엽니다. 켜지지 않으면 원인을 진단하고 그 창에서 서버를 직접 켭니다 |
| 안 열릴 때 진단 | `windows\diagnose.bat`. 결과가 바탕화면 `재무분석_진단.txt`에 저장되니 그 내용을 보내 주세요 |
| 지금 갱신 | 바탕화면 `재무분석 지금 갱신.bat` |
| 새 종목 추가 | 화면 A에서 종목코드를 넣고 "분석 실행". 또는 명령 프롬프트에서 `windows\run_now.bat 005930 000660` (여러 개 가능). 한 번 추가한 종목은 평일 자동 수집 대상에 계속 포함됩니다 |
| 기록 보기 | `%LOCALAPPDATA%\CorpAnalyzer\data\logs\run_날짜.log` |
| 인증키 바꾸기 | `install.bat`을 다시 실행합니다(작업 등록은 덮어씁니다) |
| 실행 시각 바꾸기 | 작업 스케줄러 → 작업 스케줄러 라이브러리 → `CorpAnalyzer-Update` → 트리거 |
| 제거 | `windows\uninstall.bat`. 작업과 바로가기만 지우고 DB는 남깁니다 |

## 주의

- PC 데이터는 `%LOCALAPPDATA%\CorpAnalyzer\data`에 있고, GitHub Actions는 저장소의 `data` 폴더를 씁니다. 두 DB는 서로 섞이지 않습니다. PC만 쓰신다면 GitHub에는 `DART_API_KEY`를 등록하지 않아도 됩니다.
- 가치평가 가정(위험프리미엄, 베타, 성장률 등)은 `C:\CorpAnalyzer\analyzer\assumptions.json`에서 바꿉니다. 바꾼 뒤 `재무분석 지금 갱신.bat`을 실행하면 다시 계산합니다.
- 화면 서버는 `127.0.0.1`에서만 열려서, 같은 공유기의 다른 기기에서는 접속되지 않습니다.
- 이 스크립트는 리눅스 개발 환경에서 작성했고 **실제 윈도우11에서는 아직 실행해 보지 않았습니다.** 오류가 나면 창에 나온 문구를 그대로 보내 주십시오.
