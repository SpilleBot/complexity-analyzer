@echo off
rem ============================================================
rem  Complexity Analyzer - analyze ONE PR and set its label
rem  in Azure DevOps Server (complexity:N on the PR)
rem  Usage:
rem    label-pr.bat "PR_URL"
rem    label-pr.bat "PR_URL" --dry-run   (show label, don't update)
rem ============================================================
setlocal
cd /d "%~dp0"

rem --- Credentials and server settings (edit if needed) ---
rem Do NOT hardcode real tokens here: the repo is public and GitHub blocks pushes with secrets.
rem Set them once in your environment instead:  setx AZURE_DEVOPS_PAT "..."  and  setx KIMI_API_KEY "..."
if not defined AZURE_DEVOPS_PAT set "AZURE_DEVOPS_PAT=YOUR_AZURE_DEVOPS_PAT_HERE"
if not defined KIMI_API_KEY set "KIMI_API_KEY=YOUR_KIMI_API_KEY_HERE"
set "AZURE_DEVOPS_SERVER_URL=https://prm-devtfs1.itps.com/tfs"
set "AZURE_DEVOPS_COLLECTION=ITPS"
rem Dummy GitHub token: silences the "GH_TOKEN not set" warning.
rem The source is detected from the URL, GitHub is never used here.
set "GH_TOKEN=not-used-ado-only"

set "CLI=.venv\Scripts\complexity-cli.exe"
if not exist "%CLI%" (
    echo [ERROR] Not found: %CD%\%CLI%
    echo         Run from the complexity-analyzer project folder.
    pause
    exit /b 1
)

if "%~1"=="" (
    echo Usage: label-pr.bat "PR_URL"
    echo Example:
    echo   label-pr.bat "https://prm-devtfs1.itps.com/tfs/ITPS/AVIST/_git/AVIST/pullrequest/13657"
    pause
    exit /b 1
)

echo === Analyze + set label: %~1 ===
"%CLI%" label-pr "%~1" --provider kimi %2 %3 %4

echo.
echo Exit code: %ERRORLEVEL%
pause
endlocal
