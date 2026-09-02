@echo off
setlocal EnableExtensions
cd /d "%~dp0" || exit /b 1

if defined KSAT_BUILD_PYTHON (
  set "PYTHON_EXE=%KSAT_BUILD_PYTHON%"
) else (
  set "PYTHON_EXE=%CD%\.build-venv\Scripts\python.exe"
)

if not exist "%PYTHON_EXE%" (
  where py >nul 2>nul || (
    echo Python launcher was not found. Install Python 3.10 or newer.
    exit /b 1
  )
  py -3 -m venv "%CD%\.build-venv" || exit /b 1
)

"%PYTHON_EXE%" -m pip install --disable-pip-version-check --no-input -r requirements.txt pyinstaller || exit /b 1

if defined ISCC_EXE (
  "%PYTHON_EXE%" scripts\windows_release.py all --root "%CD%" --python "%PYTHON_EXE%" --iscc "%ISCC_EXE%" || exit /b 1
) else (
  "%PYTHON_EXE%" scripts\windows_release.py all --root "%CD%" --python "%PYTHON_EXE%" || exit /b 1
)

echo.
echo Complete:
echo   release\KSATCoordinator-2.0.0.exe
echo   release\KSATClient-2.0.0.exe
echo   release\KSATCoordinatorSetup-2.0.0.exe
echo   release\KSATClientSetup-2.0.0.exe
echo   release\SHA256SUMS.txt
endlocal
