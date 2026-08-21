@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>nul || (
  echo Python launcher was not found. Install Python 3.10 or newer from python.org, then run this script again.
  exit /b 1
)

py -3 -m venv .build-venv
call .build-venv\Scripts\activate.bat
python -m pip install --upgrade pip
python -m pip install -r requirements.txt pyinstaller

set "PYINSTALLER_ROOT=%TEMP%\AptitudeLab-PyInstaller"
python -m PyInstaller --noconfirm --clean --onefile --windowed --name KSATServer ^
  --workpath "%PYINSTALLER_ROOT%\work" --specpath "%PYINSTALLER_ROOT%\spec" --distpath "%CD%\dist" ^
  --add-data "%CD%\static;static" ^
  --add-data "%CD%\templates\visual-data-interpretation.html;templates" ^
  --add-data "%CD%\templates\visual-data-interpretation.json;templates" ^
  --collect-all fastapi --collect-all starlette --collect-all uvicorn --collect-all multipart app.py
if errorlevel 1 exit /b 1

set "ISCC_EXE="
where ISCC >nul 2>nul && set "ISCC_EXE=ISCC"
if not defined ISCC_EXE if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC_EXE=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not defined ISCC_EXE if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC_EXE=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if not defined ISCC_EXE if exist "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" set "ISCC_EXE=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if not defined ISCC_EXE (
  echo.
  echo The server executable was built at dist\KSATServer.exe.
  echo Install Inno Setup 6, ensure ISCC is on PATH, then rerun this script to create the installer.
  exit /b 1
)

"%ISCC_EXE%" installer\AptitudeLab.iss
if errorlevel 1 exit /b 1
echo.
echo Complete: release\KSAT-Setup-1.2.1.exe
endlocal
