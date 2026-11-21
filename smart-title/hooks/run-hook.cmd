: << 'CMDBLOCK'
@echo off
REM Cross-platform wrapper for this plugin's hook scripts.
REM On Windows, cmd.exe invokes Python directly (no Git Bash hop, zero stderr)
REM and falls back to Git Bash when needed.
REM On macOS/Linux, POSIX sh treats ":" as a no-op, skips the heredoc, and runs
REM the shell snippet at the bottom.
REM
REM Usage: run-hook.cmd <script-name> [args...]

if "%~1"=="" (
    echo {}
    exit /b 0
)
setlocal
set "HOOK_DIR=%~dp0"
for %%I in ("%~dp0..") do set "CORE_PY=%%~fI\scripts\smart_title_core.py"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

if /I "%~1"=="smart-title-hook" (
    if "%~2"=="" (
        echo {}
        exit /b 0
    )
    if not exist "%CORE_PY%" (
        echo {}
        exit /b 0
    )
    py -3 -c "import sqlite3,sys;raise SystemExit(0 if sys.version_info>=(3,8) else 1)" <nul >nul 2>nul && (
        py -3 "%CORE_PY%" "%~2" 2>nul || echo {}
        exit /b 0
    )
    py -c "import sqlite3,sys;raise SystemExit(0 if sys.version_info>=(3,8) else 1)" <nul >nul 2>nul && (
        py "%CORE_PY%" "%~2" 2>nul || echo {}
        exit /b 0
    )
    python3 -c "import sqlite3,sys;raise SystemExit(0 if sys.version_info>=(3,8) else 1)" <nul >nul 2>nul && (
        python3 "%CORE_PY%" "%~2" 2>nul || echo {}
        exit /b 0
    )
    python -c "import sqlite3,sys;raise SystemExit(0 if sys.version_info>=(3,8) else 1)" <nul >nul 2>nul && (
        python "%CORE_PY%" "%~2" 2>nul || echo {}
        exit /b 0
    )
)

if exist "%ProgramFiles%\Git\bin\bash.exe" (
    "%ProgramFiles%\Git\bin\bash.exe" "%HOOK_DIR%%~1" %2 %3 %4 %5 %6 %7 %8 %9
    exit /b 0
)
if exist "%ProgramFiles(x86)%\Git\bin\bash.exe" (
    "%ProgramFiles(x86)%\Git\bin\bash.exe" "%HOOK_DIR%%~1" %2 %3 %4 %5 %6 %7 %8 %9
    exit /b 0
)
if exist "%LOCALAPPDATA%\Programs\Git\bin\bash.exe" (
    "%LOCALAPPDATA%\Programs\Git\bin\bash.exe" "%HOOK_DIR%%~1" %2 %3 %4 %5 %6 %7 %8 %9
    exit /b 0
)
where bash >nul 2>nul
if %ERRORLEVEL% equ 0 (
    bash "%HOOK_DIR%%~1" %2 %3 %4 %5 %6 %7 %8 %9
    exit /b 0
)

REM No Python or bash available: emit valid JSON and stay quiet so turns never fail.
echo {}
exit /b 0
CMDBLOCK

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 0
script_name=$1
[ -n "$script_name" ] || exit 0
shift
exec sh "$script_dir/$script_name" "$@"
