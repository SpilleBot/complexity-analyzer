@echo off
rem ============================================================
rem  Complexity Analyzer - check ONE PR (WITH Kimi analysis)
rem  and save the result to result<PR-number>.csv
rem  Usage:
rem    check-pr.bat "PR_URL"
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
    echo Usage: check-pr.bat "PR_URL"
    echo Example:
    echo   check-pr.bat "https://prm-devtfs1.itps.com/tfs/ITPS/AVIST/_git/AVIST/pullrequest/13657"
    pause
    exit /b 1
)

rem %~n1 = last segment of the URL path, i.e. the PR number
set "PRNUM=%~n1"
set "CSV=result%PRNUM%.csv"

rem Write the URL into a one-line input file for batch-analyze
set "URLFILE=%TEMP%\check-pr-url.txt"
echo %~1> "%URLFILE%"

echo === Analyzing PR %PRNUM% with Kimi ===
echo     Result will be saved to: %CD%\%CSV%
"%CLI%" batch-analyze --input-file "%URLFILE%" --provider kimi --output "%CSV%" --timeout 240
set "RC=%ERRORLEVEL%"
del "%URLFILE%" 2>nul

echo.
if %RC%==0 (
    echo Content of %CSV%:
    echo ----------------------------------------
    type "%CSV%"
    echo ----------------------------------------
) else (
    echo [ERROR] Analysis failed, exit code %RC%
)
echo Exit code: %RC%
pause
endlocal
