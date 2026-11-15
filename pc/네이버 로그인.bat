@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist .venv (echo 먼저 설치.bat을 실행하세요. & pause & exit /b 1)
call .venv\Scripts\activate.bat
echo 크롬 창이 열리면 네이버에 직접 로그인하세요. "로그인 상태 유지"를 켜 두면 오래 유지됩니다.
python blog_auto.py --login
pause
