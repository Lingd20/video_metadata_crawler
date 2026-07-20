@echo off
chcp 65001 >nul 2>&1
setlocal enabledelayedexpansion

set "PROJECT_ROOT=%~dp0"
set "PYTHONPATH=%PROJECT_ROOT%scripts;%PYTHONPATH%"

set "CONFIG=%PROJECT_ROOT%config\config.yaml"
set "INPUT_FILE="
set "OUT_DIR="
set "TASK_NAME="

:parse_args
if "%~1"=="" goto run
if /I "%~1"=="--config" (set "CONFIG=%~2" & shift & shift & goto parse_args)
if /I "%~1"=="--input"  (set "INPUT_FILE=%~2" & shift & shift & goto parse_args)
if /I "%~1"=="--out"    (set "OUT_DIR=%~2" & shift & shift & goto parse_args)
if /I "%~1"=="--task-name" (set "TASK_NAME=%~2" & shift & shift & goto parse_args)
if /I "%~1"=="-h" goto help
if /I "%~1"=="--help" goto help
echo 未知参数: %~1
exit /b 1

:help
echo 用法: %~nx0 [选项]
echo.
echo   --config FILE     配置文件（默认 config\config.yaml）
echo   --input FILE      输入文件
echo   --out DIR         下载根目录
echo   --task-name NAME  任务子目录名
exit /b 0

:run
echo ==========================================
echo YouTube 手动字幕 · 搜索+下载
echo ==========================================
echo 配置文件: %CONFIG%

set "CMD=python "%PROJECT_ROOT%scripts\subtitled_search_download\run.py" --config "%CONFIG%""
if defined INPUT_FILE  if not "%INPUT_FILE%"=="" set "CMD=!CMD! --input "%INPUT_FILE%""
if defined OUT_DIR     if not "%OUT_DIR%"==""     set "CMD=!CMD! --out "%OUT_DIR%""
if defined TASK_NAME   if not "%TASK_NAME%"==""   set "CMD=!CMD! --task-name "%TASK_NAME%""

%CMD%

echo ==========================================
echo 执行完毕
echo ==========================================
