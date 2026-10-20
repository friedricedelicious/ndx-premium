@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 正在抓取行情与净值，请稍候 ...
where python >nul 2>nul
if errorlevel 1 (
  echo 没找到 python，请到 https://www.python.org 安装后再试
  pause
  exit /b 1
)
python run.py
if errorlevel 1 (
  echo.
  echo 抓取失败，看看上面的报错
  pause
  exit /b 1
)
echo.
echo 完成，正在打开页面 ...
start "" "%~dp0docs\index.html"
pause
