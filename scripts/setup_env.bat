@echo off
chcp 936 >nul
setlocal
cd /d "%~dp0.."

echo ============================================
echo   工时识别核算系统 - 环境安装
echo ============================================
echo.

if not exist ".venv\Scripts\python.exe" (
  echo [1/4] 查找 Python 3.12 ...
  where py >nul 2>nul
  if errorlevel 1 (
    echo [ERROR] 未找到 py 启动器，请先安装 Python 3.11 或 3.12
    echo         下载: https://www.python.org/downloads/
    pause
    exit /b 1
  )
  py -3.12 -m venv .venv
  if errorlevel 1 (
    py -3.11 -m venv .venv
  )
  if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] 创建虚拟环境失败，请确认已安装 Python 3.11/3.12
    pause
    exit /b 1
  )
)

echo [2/4] 安装 Python 依赖（首次约 1GB，请耐心等待）...
.venv\Scripts\python.exe -m pip install -U pip
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 (
  echo [ERROR] Python 依赖安装失败
  pause
  exit /b 1
)

echo [3/4] 安装前端依赖 ...
if exist "frontend\package.json" (
  pushd frontend
  call npm install --no-audit --no-fund
  popd
)

echo [4/4] 初始化数据库与字典 ...
.venv\Scripts\python.exe -c "from app.db import init_db; from app.seed import seed_all; from app.db import SessionLocal; init_db(); db=SessionLocal(); print(seed_all(db)); db.close()"

echo.
echo ============================================
echo   安装完成
echo   后端启动: scripts\start_backend.bat
echo   前端开发: scripts\start_frontend.bat
echo ============================================
pause
