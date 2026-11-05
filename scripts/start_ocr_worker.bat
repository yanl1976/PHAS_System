@echo off
chcp 936 >nul
setlocal
cd /d "%~dp0.."

echo ============================================
echo   工时识别核算系统 - OCR 识别进程
echo ============================================
echo.

if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] 未找到虚拟环境 .venv
  echo 请先执行: scripts\setup_env.bat
  pause
  exit /b 1
)

echo 队列在数据库里（image.status），本进程是唯一领取者。
echo 只能开一个：多开不会更快，只会抢 CPU 并各占约 400MB 内存。
echo.
echo 队列现状可随时查看: .venv\Scripts\python.exe scripts\ocr_worker.py --status
echo 按 Ctrl+C 停止（会处理完当前这张再退出）
echo.

.venv\Scripts\python.exe scripts\ocr_worker.py %*
pause
