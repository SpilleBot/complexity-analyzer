@echo off
rem ============================================================
rem  Complexity Analyzer - Azure DevOps Server + Kimi
rem  Usage:
rem    analyze.bat                          -> batch analysis (edit dates below)
rem    analyze.bat "PR_URL"                 -> analyze one PR
rem    analyze.bat "PR_URL" --dry-run       -> fetch diff only, no LLM call
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
set "ADO_PROJECT=AVIST"
rem Dummy GitHub token: silences the "GH_TOKEN not set" warning.
rem The source is detected from the URL, GitHub is never used here.
set "GH_TOKEN=not-used-ado-only"

rem --- Batch mode date range (used when no URL argument) ---
set "SINCE=2026-10-01"
set "UNTIL=2026-10-05"

set "CLI=.venv\Scripts\complexity-cli.exe"
if not exist "%CLI%" (
    echo [ERROR] Not found: %CD%\%CLI%
    echo         Run from the complexity-analyzer project folder.
    pause
    exit /b 1
)

if not "%~1"=="" goto SINGLE

echo === Batch analysis: project %ADO_PROJECT%, %SINCE%..%UNTIL% ===
echo     Output: %CD%\results.csv  (safe to interrupt, resume is on)
"%CLI%" batch-analyze --project %ADO_PROJECT% --since %SINCE% --until %UNTIL% --provider kimi --output results.csv --timeout 240
goto DONE

:SINGLE
echo === Analyzing PR: %~1 ===
"%CLI%" analyze-pr "%~1" --provider kimi %2 %3 %4 %5 %6

:DONE
echo.
echo Exit code: %ERRORLEVEL%
pause
endlocal
