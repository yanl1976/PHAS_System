@echo off
chcp 936 >nul
setlocal
cd /d "%~dp0.."

echo ============================================
echo   工时识别核算系统 - 一键启动
echo ============================================
echo.

if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] 未找到虚拟环境 .venv
  echo 请先执行: scripts\setup_env.bat
  pause
  exit /b 1
)

echo [1/2] 启动 OCR 识别进程（会先做启动自愈与引擎预热，约 4 秒）
start "工时系统-OCR识别" cmd /k "scripts\start_ocr_worker.bat"
timeout /t 3 /nobreak >nul

echo [2/2] 启动 Web 服务
start "工时系统-Web" cmd /k "scripts\start_backend.bat"
timeout /t 3 /nobreak >nul

echo.
echo 两个窗口已打开。服务地址: http://127.0.0.1:8000
echo 健康检查:                   http://127.0.0.1:8000/api/health
echo 关闭本窗口不影响已启动的服务。
echo.
pause
