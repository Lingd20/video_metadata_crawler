@echo off
chcp 65001 >nul 2>&1
setlocal enabledelayedexpansion

set "PROJECT_ROOT=%~dp0"
set "PYTHONPATH=%PROJECT_ROOT%scripts;%PYTHONPATH%"

set "CONFIG=%PROJECT_ROOT%config\config.yaml"
set "INPUT_FILE="
set "OUTPUT_FILE="

:parse_args
if "%~1"=="" goto run
if /I "%~1"=="--config" (set "CONFIG=%~2" & shift & shift & goto parse_args)
if /I "%~1"=="--input"  (set "INPUT_FILE=%~2" & shift & shift & goto parse_args)
if /I "%~1"=="--output" (set "OUTPUT_FILE=%~2" & shift & shift & goto parse_args)
if /I "%~1"=="-h" goto help
if /I "%~1"=="--help" goto help
echo 未知参数: %~1
exit /b 1

:help
echo 用法: %~nx0 [选项]
echo.
echo 选项:
echo   --config FILE    配置文件路径 (默认: config\config.yaml)
echo   --input FILE     输入文件路径 (覆盖配置文件)
echo   --output FILE    输出文件路径 (覆盖配置文件)
echo   -h, --help       显示帮助
exit /b 0

:run
echo ==========================================
echo Video Metadata Crawler
echo ==========================================
echo 配置文件: %CONFIG%

set "CMD=python "%PROJECT_ROOT%scripts\crawl_all.py" --config "%CONFIG%""
if defined INPUT_FILE  if not "%INPUT_FILE%"=="" set "CMD=!CMD! --input "%INPUT_FILE%""
if defined OUTPUT_FILE if not "%OUTPUT_FILE%"=="" set "CMD=!CMD! --output "%OUTPUT_FILE%""

%CMD%

echo ==========================================
echo 执行完毕
echo ==========================================
