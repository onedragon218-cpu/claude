# 윈도우11 PC에서 자동 실행하기

설치하면 PC에서 아래 두 가지가 자동으로 돌아갑니다.

| 작업(작업 스케줄러 이름) | 언제 | 하는 일 |
|---|---|---|
| `CorpAnalyzer-Update` | 평일 07:13. PC가 꺼져 있었으면 켜진 직후 | DB에 없는 값, 새 공시·정정 공시, 컨센서스(7일마다), 기준일 주가만 수집하고 화면을 다시 만듭니다 |
| `CorpAnalyzer-Server` | 윈도우에 로그인할 때 | 화면 서버를 시작합니다. 주소는 http://localhost:8765 이고, 이 PC에서만 열립니다 |

## 설치 순서

1. **프로그램 받기**: 둘 중 하나를 고릅니다.
   - Git이 있으면 아래 명령을 실행합니다.

     ```bash
     git clone -b claude/zen-lamport-1jplcv https://github.com/onedragon218-cpu/claude.git C:\CorpAnalyzer
     ```

   - Git이 없으면 GitHub 저장소 화면에서 브랜치 `claude/zen-lamport-1jplcv`를 고르고 Code → Download ZIP을 누른 뒤, `C:\CorpAnalyzer` 같은 폴더에 압축을 풉니다. 바탕화면이나 OneDrive 폴더는 피하는 것이 좋습니다.
2. **DART 인증키 준비**: https://opendart.fss.or.kr → 인증키 신청 (무료)
3. `windows\install.bat`을 **더블클릭**합니다.
   - "Windows의 PC 보호" 창이 뜨면 → 추가 정보 → 실행을 누릅니다.
   - 파이썬이 없으면 winget으로 Python 3.12를 자동 설치합니다. 사용자 계정에만 설치하며 관리자 권한은 필요 없습니다.
   - 인증키를 붙여 넣고 Enter를 누릅니다. 키는 `%APPDATA%\corp-analyzer\dart_api_key.txt`에 저장되며, 프로그램 폴더 밖이라 Git에 올라가지 않습니다.
   - 첫 수집이 끝나면 브라우저가 열립니다.
4. 바탕화면에 바로가기 두 개가 생깁니다.
   - `재무분석 열기`: 화면을 엽니다.
   - `재무분석 지금 갱신.bat`: 지금 바로 수집하고 화면을 엽니다.

## 자주 쓰는 동작

| 하고 싶은 일 | 방법 |
|---|---|
| 화면 보기 | 바탕화면 `재무분석 열기` 또는 `windows\open.bat` |
| 지금 갱신 | 바탕화면 `재무분석 지금 갱신.bat` |
| 새 종목 추가 | 화면 A에서 종목코드를 넣고 "분석 실행". 또는 명령 프롬프트에서 `windows\run_now.bat 005930` |
| 기록 보기 | `data\logs\run_날짜.log` |
| 인증키 바꾸기 | `install.bat`을 다시 실행합니다(작업 등록은 덮어씁니다) |
| 실행 시각 바꾸기 | 작업 스케줄러 → 작업 스케줄러 라이브러리 → `CorpAnalyzer-Update` → 트리거 |
| 제거 | `windows\uninstall.bat`. 작업과 바로가기만 지우고 DB는 남깁니다 |

## 주의

- **PC와 GitHub Actions 중 하나만 자동 수집에 쓰십시오.** 둘 다 같은 `data\analyzer.db`를 고치면 DB 파일이 서로 충돌합니다. PC에서 돌릴 거라면 GitHub에는 `DART_API_KEY`를 등록하지 않거나, Actions 화면에서 워크플로를 Disable하십시오.
- 화면 서버는 `127.0.0.1`에서만 열려서, 같은 공유기의 다른 기기에서는 접속되지 않습니다.
- 이 스크립트는 리눅스 개발 환경에서 작성했고 **실제 윈도우11에서는 아직 실행해 보지 않았습니다.** 오류가 나면 창에 나온 문구를 그대로 보내 주십시오.
