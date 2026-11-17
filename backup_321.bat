@echo off
REM ==========================================================================
REM  backup_321.bat - backup rule 3-2-1 for the local stenographer kit.
REM
REM  Copy 1: local disk      (LOCAL_ROOT)
REM  Copy 2: external disk   (EXTERNAL_ROOT)
REM  Copy 3: Tailscale node (TAILSCALE_ROOT, UNC path)
REM
REM  Each copy is verified with certutil -hashfile (SHA256): the hash of the
REM  source file is compared with the hash of the copied file. Old copies are
REM  rotated after RETENTION_DAYS days. The script never asks questions and
REM  returns a non-zero exit code on any error.
REM
REM  NOTE: every command below is ASCII-only (no Cyrillic) so the script runs
REM  under any console code page. Configure paths via environment variables
REM  (BA_BACKUP_SRC, BA_BACKUP_LOCAL, BA_BACKUP_EXTERNAL, BA_BACKUP_TAILSCALE).
REM ==========================================================================

setlocal EnableExtensions EnableDelayedExpansion

set "SRC_DIR=%BA_BACKUP_SRC%"
if "%SRC_DIR%"=="" set "SRC_DIR=%USERPROFILE%\Documents\Stenographer"

set "LOCAL_ROOT=%BA_BACKUP_LOCAL%"
if "%LOCAL_ROOT%"=="" set "LOCAL_ROOT=%USERPROFILE%\Backups\Stenographer\local"

set "EXTERNAL_ROOT=%BA_BACKUP_EXTERNAL%"
if "%EXTERNAL_ROOT%"=="" set "EXTERNAL_ROOT=E:\Backups\Stenographer\external"

set "TAILSCALE_ROOT=%BA_BACKUP_TAILSCALE%"
if "%TAILSCALE_ROOT%"=="" set "TAILSCALE_ROOT=\\ts-node\home\stenographer"

set "RETENTION_DAYS=30"

set "STAMP="
for /f "usebackq delims=" %%i in (`powershell -NoProfile -Command "Get-Date -Format yyyyMMdd-HHmmss" 2^>nul`) do set "STAMP=%%i"
if "%STAMP%"=="" set "STAMP=backup-%RANDOM%"

echo [backup] source    : %SRC_DIR%
echo [backup] copy 1    : %LOCAL_ROOT%
echo [backup] copy 2    : %EXTERNAL_ROOT%
echo [backup] copy 3    : %TAILSCALE_ROOT%
echo [backup] retention : %RETENTION_DAYS% days
echo [backup] stamp     : %STAMP%

if not exist "%SRC_DIR%" (
    echo [backup] ERROR: source directory not found: %SRC_DIR%
    exit /b 2
)

call :make_backup "%LOCAL_ROOT%" "copy 1 (local)"
if errorlevel 1 exit /b 3

call :make_backup "%EXTERNAL_ROOT%" "copy 2 (external)"
if errorlevel 1 exit /b 4

call :make_backup "%TAILSCALE_ROOT%" "copy 3 (tailscale)"
if errorlevel 1 exit /b 5

echo [backup] DONE: three copies created and verified.
exit /b 0


REM --------------------------------------------------------------------------
REM  :make_backup <target root> <label>
REM  Copies the source tree, verifies every file with SHA256, rotates old files.
REM --------------------------------------------------------------------------
:make_backup
set "TARGET=%~1"
set "LABEL=%~2"

echo.
echo [backup] --- %LABEL% to %TARGET%

if not exist "%TARGET%" (
    mkdir "%TARGET%" 2>nul
    if not exist "%TARGET%" (
        echo [backup] ERROR: cannot create target: %TARGET%
        exit /b 1
    )
)

rem robocopy: 0-7 are success codes, 8 and above are failures.
REM ВНИМАНИЕ: цель указываем БЕЗ обратного слэша в конце — иначе кавычка
REM закрывается неверно и robocopy получает мусорные аргументы.
robocopy "%SRC_DIR%" "%TARGET%" /E /R:1 /W:1 /NFL /NDL /NJH /NJS /NP /XD "%TARGET%" >nul
set "RC=!ERRORLEVEL!"
if !RC! GEQ 8 (
    echo [backup] ERROR: copy failed with robocopy code !RC!
    exit /b 1
)

call :verify_tree "%SRC_DIR%" "%TARGET%"
if errorlevel 1 (
    echo [backup] ERROR: hash verification failed for %TARGET%
    exit /b 1
)

REM Ротация: удаляем файлы старше RETENTION_DAYS дней.
REM Путь передаётся через переменную окружения, чтобы не ломаться о пробелы.
set "BA_ROTATE_PATH=%TARGET%"
powershell -NoProfile -Command "Get-ChildItem -LiteralPath $env:BA_ROTATE_PATH -Recurse -File -ErrorAction SilentlyContinue | Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-%RETENTION_DAYS%) } | Remove-Item -Force -ErrorAction SilentlyContinue" >nul 2>&1
echo [backup] rotated files older than %RETENTION_DAYS% days in %TARGET%
exit /b 0


REM --------------------------------------------------------------------------
REM  :verify_tree <source> <target>
REM  Compares SHA256 of every source file with the hash of its copy.
REM --------------------------------------------------------------------------
:verify_tree
set "VSRC=%~1"
set "VTGT=%~2"
set "MISMATCH=0"
set "COUNT=0"

for /f "usebackq delims=" %%F in (`dir /b /s /a-d "%VSRC%"`) do (
    set "REL=%%F"
    set "REL=!REL:%VSRC%\=!"
    call :hash_of "%VSRC%\!REL!"
    set "H1=!HASH!"
    call :hash_of "%VTGT%\!REL!"
    set "H2=!HASH!"
    if not "!H1!"=="" (
        set /a COUNT+=1
        if not "!H1!"=="!H2!" (
            echo [backup] HASH MISMATCH: !REL!
            set "MISMATCH=1"
        )
    )
)

if !MISMATCH! EQU 1 exit /b 1
echo [backup] verified !COUNT! file(s) in %VTGT%
exit /b 0


REM --------------------------------------------------------------------------
REM  :hash_of <path> - sets HASH to the SHA256 hash of the file (empty if none)
REM --------------------------------------------------------------------------
:hash_of
set "HASH="
for /f "usebackq tokens=1" %%H in (`certutil -hashfile "%~1" SHA256 2^>nul ^| findstr /R /C:"^[0-9a-fA-F][0-9a-fA-F]*$"`) do set "HASH=%%H"
exit /b 0
