@echo off
cd /d "%~dp0"
set "BUILD_DIR=%TEMP%\DirtTownModLoaderBuild-%RANDOM%-%RANDOM%"
set "DIST_DIR=%~dp0dist\v0.7.1"
py -3 -m venv .venv || goto fail
.venv\Scripts\python.exe -m pip install pyinstaller || goto fail
.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean --onefile --console --name modloader_dt --distpath "%DIST_DIR%" --workpath "%BUILD_DIR%\work" --specpath "%BUILD_DIR%\spec" modloader.py || goto fail
copy /y "%DIST_DIR%\modloader_dt.exe" "%~dp0..\modloader_dt.exe" || goto fail
echo EXE: %~dp0..\modloader_dt.exe
pause
exit /b 0
:fail
echo Build failed.
pause
exit /b 1
