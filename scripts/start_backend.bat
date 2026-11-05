@echo off
chcp 936 >nul
setlocal
cd /d "%~dp0.."

echo ============================================
echo   工时识别核算系统 - 启动 Web 服务（多进程）
echo ============================================
echo.

if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] 未找到虚拟环境 .venv
  echo 请先执行: scripts\setup_env.bat
  pause
  exit /b 1
)

rem 进程数默认 4。12 核实测 4 进程对 8 并发有 5~7 倍收益；
rem 再多会先撞内存（每进程约 120MB，本机可用内存只有 7GB）。
if "%WT_WEB_WORKERS%"=="" set WT_WEB_WORKERS=4

echo 服务地址: http://127.0.0.1:8000
echo Web 进程数: %WT_WEB_WORKERS%
echo.
echo [重要] 识别任务由独立进程执行，本窗口不含 OCR 引擎。
echo        请另开窗口运行 scripts\start_ocr_worker.bat
echo        或直接运行 scripts\start_all.bat 一次起两个。
echo.
echo 按 Ctrl+C 停止
echo.

.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers %WT_WEB_WORKERS%
pause
