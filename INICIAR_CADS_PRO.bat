@echo off
REM Inicializador do projeto. A chave e solicitada sem eco pelo PowerShell.
title CADS Pro - Inicializacao Segura
where powershell.exe >nul 2>nul
if errorlevel 1 (
    echo ERRO: PowerShell 5.1 ou superior nao encontrado.
    pause
    exit /b 1
)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0iniciar_cads_pro.ps1"
set "CADS_EXIT_CODE=%ERRORLEVEL%"
if not "%CADS_EXIT_CODE%"=="0" (
    echo Inicializacao encerrada com codigo %CADS_EXIT_CODE%.
    pause
)
exit /b %CADS_EXIT_CODE%
