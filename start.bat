@echo off
chcp 65001 >nul
cd /d "%~dp0backend"
if not exist ".venv\" (
  echo [setup] 首次运行, 创建 venv 并安装依赖...
  python -m venv .venv
  call .venv\Scripts\activate.bat
  pip install -r requirements.txt
) else (
  call .venv\Scripts\activate.bat
)
echo.
echo ============================================
echo   小红书视频解析  http://127.0.0.1:8000
echo   Ctrl+C 退出
echo ============================================
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
pause
