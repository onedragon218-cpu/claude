@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo [1/3] Python 확인
where py >nul 2>nul && (set PY=py -3) || (set PY=python)
%PY% --version || (echo Python 3.10 이상을 먼저 설치하세요: https://www.python.org/downloads/  설치 화면에서 "Add python.exe to PATH" 체크 & pause & exit /b 1)
echo [2/3] 가상환경 만들기(.venv)
if not exist .venv %PY% -m venv .venv
echo [3/3] 라이브러리 설치
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\python -m pip install -r requirements.txt
echo.
echo 설치 끝. run_fintrack.bat 을 더블클릭해 실행하세요.
pause
