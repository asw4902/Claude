@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo [1/3] 파이썬 확인
where py >nul 2>nul && (set PY=py -3) || (set PY=python)
%PY% --version || (echo 파이썬이 없습니다. https://www.python.org/downloads/ 에서 설치하고 "Add python.exe to PATH"를 체크하세요. & pause & exit /b 1)
echo [2/3] 필요한 프로그램 설치 (몇 분 걸립니다)
if not exist .venv %PY% -m venv .venv
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip >nul
pip install -r requirements.txt || (echo 설치 실패 & pause & exit /b 1)
python -m playwright install chromium
echo [3/3] 완료! 이제 "네이버 로그인.bat"을 한 번 실행해 로그인하고, "실행.bat"으로 시작하세요.
pause
