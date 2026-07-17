@echo off
chcp 65001 >nul
setlocal

rem === settings (edit if needed) ===
set "REPO_URL=https://github.com/katu09161004/nursing-worktime.git"
set "BRANCH=main"
set "MSG=Add nursing workload logger"

echo ============================================
echo   Push nursing-worktime to GitHub
echo ============================================
echo.

where git >nul 2>&1
if errorlevel 1 (
  echo [ERROR] git not found. Install Git for Windows: https://git-scm.com/
  goto end
)

if not exist "main.py" (
  echo [ERROR] main.py not found in this folder.
  echo         Put this file inside the nursing-worktime folder and run again.
  goto end
)

if not exist ".git" git init

rem --- ensure git identity is set (one time) ---
git config user.email >nul 2>&1
if not errorlevel 1 goto have_id
echo Git identity is not set yet. Please enter it once.
set /p "GEMAIL=  Email (e.g. katu09161004@users.noreply.github.com): "
set /p "GNAME=  Name  (e.g. katu09161004): "
git config --global user.email "%GEMAIL%"
git config --global user.name "%GNAME%"
:have_id

git add -A
git commit -m "%MSG%"
git branch -M %BRANCH%

git remote get-url origin >nul 2>&1
if errorlevel 1 (
  git remote add origin "%REPO_URL%"
) else (
  git remote set-url origin "%REPO_URL%"
)

echo.
echo Pushing to %REPO_URL% ...
git push -u origin %BRANCH%
set "PUSHRC=%errorlevel%"

echo.
if "%PUSHRC%"=="0" goto ok
goto fail

:ok
echo [OK] Done.
echo   Repo    : https://github.com/katu09161004/nursing-worktime
echo   Next    : enable GitHub Pages  [Settings - Pages - Branch: main  folder: /docs]
echo   Preview : https://katu09161004.github.io/nursing-worktime/
goto end

:fail
echo [!] push failed. Read the message above.
echo     - Create an EMPTY public repo named nursing-worktime on GitHub first.
echo     - Sign in when the credential window appears.
goto end

:end
echo.
pause
