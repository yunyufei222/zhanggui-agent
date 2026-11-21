@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo [x] 没找到 .venv，先建虚拟环境并装依赖：
  echo     python -m venv .venv ^&^& .venv\Scripts\python.exe -m pip install -e .
  exit /b 1
)

if not exist ".env" (
  echo [!] 没有 .env，数据库口令将读取环境变量 DB_PASSWORD
)

if /i "%~1"=="--check" (
  echo [ok] .venv 就位
  if exist ".env" (echo [ok] .env 就位) else (echo [!] 缺 .env)
  .venv\Scripts\python.exe seed_demo.py
  exit /b %errorlevel%
)

echo 启动两个服务（各占一个窗口，关窗即停）...
start "zhanggui-main :8111" .venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8111
start "zhanggui-agent :8112" .venv\Scripts\python.exe -m uvicorn agent.app:app --host 127.0.0.1 --port 8112

echo.
echo   主应用（订单/支付/工单/退款）  http://127.0.0.1:8111/docs
echo   客服 Agent（对话/会话）        http://127.0.0.1:8112/docs
echo   十条断言验收                   .venv\Scripts\python.exe e2e.py
echo.

timeout /t 3 >nul
endlocal
