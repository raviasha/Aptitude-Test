@echo off
setlocal EnableExtensions
cd /d "%~dp0" || exit /b 1

if not defined KSAT_SIGNING_PFX (
  echo KSAT_SIGNING_PFX is required for a production release.
  exit /b 1
)
if not defined KSAT_SIGNING_PFX_PASSWORD (
  echo KSAT_SIGNING_PFX_PASSWORD is required for a production release.
  exit /b 1
)
if not defined KSAT_SIGNING_PUBLISHER (
  echo KSAT_SIGNING_PUBLISHER is required for a production release.
  exit /b 1
)
if not defined KSAT_SIGNING_TIMESTAMP_URL (
  echo KSAT_SIGNING_TIMESTAMP_URL is required for a production release.
  exit /b 1
)

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
  "%PYTHON_EXE%" scripts\windows_release.py all --root "%CD%" --python "%PYTHON_EXE%" --iscc "%ISCC_EXE%" --signing-pfx "%KSAT_SIGNING_PFX%" --signing-publisher "%KSAT_SIGNING_PUBLISHER%" --timestamp-url "%KSAT_SIGNING_TIMESTAMP_URL%" || exit /b 1
) else (
  "%PYTHON_EXE%" scripts\windows_release.py all --root "%CD%" --python "%PYTHON_EXE%" --signing-pfx "%KSAT_SIGNING_PFX%" --signing-publisher "%KSAT_SIGNING_PUBLISHER%" --timestamp-url "%KSAT_SIGNING_TIMESTAMP_URL%" || exit /b 1
)

echo.
echo Complete:
echo   release\KSATCoordinator-2.0.0.exe
echo   release\KSATClient-2.0.0.exe
echo   release\KSATCoordinatorSetup-2.0.0.exe
echo   release\KSATClientSetup-2.0.0.exe
echo   release\SHA256SUMS.txt
endlocal
