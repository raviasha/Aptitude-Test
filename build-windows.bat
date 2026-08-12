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

python -m PyInstaller --noconfirm --clean --onefile --windowed --name AptitudeLabServer ^
  --add-data "static;static" --add-data "templates;templates" ^
  --collect-all fastapi --collect-all starlette --collect-all uvicorn --collect-all multipart app.py

set "ISCC_EXE="
where ISCC >nul 2>nul && set "ISCC_EXE=ISCC"
if not defined ISCC_EXE if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC_EXE=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not defined ISCC_EXE if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC_EXE=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if not defined ISCC_EXE (
  echo.
  echo The server executable was built at dist\AptitudeLabServer.exe.
  echo Install Inno Setup 6, ensure ISCC is on PATH, then rerun this script to create the installer.
  exit /b 1
)

"%ISCC_EXE%" installer\AptitudeLab.iss
echo.
echo Complete: release\Aptitude-Lab-Setup.exe
endlocal
