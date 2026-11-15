@echo off
chcp 65001 >nul
echo 최신 이슈를 찾아 글쓰기를 시작합니다. (실행.bat이 켜져 있어야 합니다)
curl -s -X POST -H "Content-Type: application/json" -d "{}" http://localhost:8765/api/autorun
if errorlevel 1 (
  echo 프로그램에 연결하지 못했습니다. 먼저 실행.bat을 켜 주세요.
) else (
  echo.
  echo 시작했습니다. 몇 분 뒤 검토 앱의 검토·발행 탭에서 확인하세요.
)
pause
