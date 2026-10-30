@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" goto check_deps
py -3.11 --version >nul 2>&1
if errorlevel 1 (
  echo 请安装 Python 3.11 64 位，保留 Python Launcher 和 Tcl/Tk 选项。
  echo 下载地址：https://www.python.org/downloads/windows/
  pause
  exit /b 1
)
py -3.11 -m venv .venv
if errorlevel 1 goto failed
:check_deps
.venv\Scripts\python.exe -c "import importlib.metadata as m; from pathlib import Path; assert Path('.venv/requirements.installed').read_bytes()==Path('requirements.txt').read_bytes(); import rembg, tkinter, PIL, numpy" >nul 2>&1
if not errorlevel 1 goto launch
echo 首次安装本地依赖，请保持联网……
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 goto failed
copy /y requirements.txt .venv\requirements.installed >nul
:launch
.venv\Scripts\python.exe app.py
if errorlevel 1 goto failed
exit /b 0
:failed
echo 启动失败，请保留上方错误信息。检查 Python 版本和网络后重试。
pause
exit /b 1
