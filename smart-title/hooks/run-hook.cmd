: << 'CMDBLOCK'
@echo off
REM Cross-platform wrapper for this plugin's hook scripts (same pattern as the
REM official "design" plugin). cmd.exe runs the batch part below, which finds
REM Git Bash and hands the request to the POSIX sh part; on macOS/Linux the
REM shell treats ":" as a no-op and skips the heredoc.
REM
REM Usage: run-hook.cmd <script-name> [args...]

if "%~1"=="" exit /b 0
set "HOOK_DIR=%~dp0"

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

REM No bash available: stay quiet so the session is never blocked.
exit /b 0
CMDBLOCK

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 0
script_name=$1
[ -n "$script_name" ] || exit 0
shift
exec sh "$script_dir/$script_name" "$@"
