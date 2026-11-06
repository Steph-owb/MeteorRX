@echo off
rem ==========================================================================
rem  MeteorRX - build the portable Windows package
rem  No compiler, no PyInstaller: official Python embeddable runtime (signed by
rem  the Python Software Foundation) + packages from PyPI + MeteorRX sources.
rem  Needs Windows 10 1803+ (curl.exe, tar.exe) and Internet access.
rem  Result: dist\MeteorRX\  (portable folder)  and  dist\MeteorRX-x.y.z-win64.zip
rem ==========================================================================
setlocal
cd /d "%~dp0"
set "PYVER=3.12.10"
set "PYTAG=python312"
set "OUT=%~dp0dist\MeteorRX"
set "RT=%OUT%\runtime"
set "TMPD=%TEMP%\meteorrx_build"

where curl >nul 2>nul || (echo curl.exe not found - Windows 10 version 1803 or later is required.& goto :err)
where tar  >nul 2>nul || (echo tar.exe not found - Windows 10 version 1803 or later is required.& goto :err)

echo [1/6] Preparing %OUT% ...
if exist "%OUT%\meteorrx.ini" copy /y "%OUT%\meteorrx.ini" "%TEMP%\meteorrx.ini.keep" >nul
rem data\ (calibrations, receiver profiles, session counter) is set aside, never deleted
if exist "%OUT%\data" if not exist "%~dp0_keep_data" move "%OUT%\data" "%~dp0_keep_data" >nul
if exist "%OUT%\data" (echo %OUT%\data could not be set aside [%~dp0_keep_data exists]: move it back by hand.& goto :err)
if exist "%OUT%" rmdir /s /q "%OUT%"
if exist "%TMPD%" rmdir /s /q "%TMPD%"
mkdir "%RT%" "%TMPD%" || goto :err
if exist "%TEMP%\meteorrx.ini.keep" move /y "%TEMP%\meteorrx.ini.keep" "%OUT%\meteorrx.ini" >nul
if exist "%~dp0_keep_data" move "%~dp0_keep_data" "%OUT%\data" >nul

echo [2/6] Downloading Python %PYVER% embeddable runtime from python.org ...
curl -fsSL --retry 3 -o "%TMPD%\py.zip" "https://www.python.org/ftp/python/%PYVER%/python-%PYVER%-embed-amd64.zip" || goto :err
tar -xf "%TMPD%\py.zip" -C "%RT%" || goto :err

echo [3/6] Verifying the digital signature of the runtime ...
powershell -NoProfile -Command "$s=Get-AuthenticodeSignature '%RT%\pythonw.exe'; Write-Host ('      ' + $s.Status + ' : ' + $s.SignerCertificate.Subject); if($s.Status -ne 'Valid'){exit 1}" || goto :err
(echo %PYTAG%.zip& echo .& echo Lib\site-packages& echo ..\app& echo import site)> "%RT%\%PYTAG%._pth"

echo [4/6] Installing pip and the packages (NumPy, Numba, PySide6, sgp4, certifi) ...
curl -fsSL --retry 3 -o "%TMPD%\get-pip.py" "https://bootstrap.pypa.io/get-pip.py" || goto :err
"%RT%\python.exe" "%TMPD%\get-pip.py" --no-warn-script-location -q || goto :err
"%RT%\python.exe" -m pip install --no-warn-script-location -q -r requirements.txt || goto :err

echo [5/6] Drivers, assembling, compiling the DSP kernels and running the self-test ...
"%RT%\python.exe" tools\fetch_drivers.py drivers || goto :err
"%RT%\python.exe" tools\make_portable.py "%OUT%" || goto :err

echo [6/6] Shortcuts ...
if not defined CI call "%OUT%\Create shortcuts.cmd"

rmdir /s /q "%TMPD%" 2>nul
echo.
echo  Done.  Start MeteorRX with the shortcut, or %OUT%\MeteorRX.cmd
echo  To install on another PC: unzip dist\MeteorRX-*-win64.zip anywhere, then run
echo  "Create shortcuts.cmd" in the unzipped folder.
echo.
if not defined CI pause
exit /b 0

:err
echo.
echo  BUILD FAILED - see the messages above.
if not defined CI pause
exit /b 1
