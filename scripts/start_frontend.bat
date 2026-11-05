@echo off
chcp 936 >nul
setlocal
cd /d "%~dp0..\frontend"

echo ============================================
echo   工时识别核算系统 - 启动前端（开发模式）
echo ============================================
echo 需先启动后端 start_backend.bat
echo 访问地址: http://127.0.0.1:5173
echo.

if not exist "node_modules" (
  echo [INFO] 首次运行，正在安装前端依赖 ...
  call npm install --no-audit --no-fund
)

call npm run dev
pause
