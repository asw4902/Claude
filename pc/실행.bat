@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist .venv (echo 먼저 설치.bat을 실행하세요. & pause & exit /b 1)
call .venv\Scripts\activate.bat
python blog_auto.py
pause
