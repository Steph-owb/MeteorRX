@echo off
rem ==========================================================================
rem  MeteorRX - build MeteorRX.exe (self-contained folder + zip)
rem  Needs Windows 10 1803+ / 11 and Internet access. Python 3.12 x64 is used if
rem  installed, otherwise the official installer is downloaded from python.org and
rem  installed privately (no PATH change). The PyInstaller bootloader is compiled
rem  from source with Visual Studio Build Tools; if they are missing the script
rem  stops and offers to install them (or to continue with the stock bootloader).
rem  Optional code signing: set METEORRX_SIGN_PFX and METEORRX_SIGN_PASSWORD.
rem  Result: dist\MeteorRX\MeteorRX.exe  and  dist\MeteorRX-x.y.z-win64.zip
rem ==========================================================================
setlocal EnableExtensions
cd /d "%~dp0"
set "PYVER=3.12.10"
set "PRIV=%LOCALAPPDATA%\MeteorRX-build\python312"
set "VENV=%~dp0.venv-build"

echo [1/7] Python 3.12 x64 ...
set "PY="
set "PYCHK=import struct,sys; ok=sys.version_info[:2]==(3,12) and struct.calcsize('P')==8; print(sys.executable if ok else str())"
if exist "%PRIV%\python.exe" set "PY=%PRIV%\python.exe"
if not defined PY for /f "delims=" %%i in ('py -3.12 -c "%PYCHK%" 2^>nul') do set "PY=%%i"
if not defined PY for /f "delims=" %%i in ('python -c "%PYCHK%" 2^>nul') do set "PY=%%i"
if not defined PY (
  echo       not found - downloading the official installer from python.org
  curl -fsSL --retry 3 -o "%TEMP%\meteorrx-python.exe" "https://www.python.org/ftp/python/%PYVER%/python-%PYVER%-amd64.exe" || goto :err
  powershell -NoProfile -Command "$s=Get-AuthenticodeSignature '%TEMP%\meteorrx-python.exe'; Write-Host ('      ' + $s.Status + ' : ' + $s.SignerCertificate.Subject); if($s.Status -ne 'Valid'){exit 1}" || goto :err
  "%TEMP%\meteorrx-python.exe" /quiet InstallAllUsers=0 PrependPath=0 Include_launcher=0 Include_test=0 Include_doc=0 Include_tcltk=0 Shortcuts=0 AssociateFiles=0 TargetDir="%PRIV%" || goto :err
  set "PY=%PRIV%\python.exe"
)
echo       %PY%

echo [2/7] Build environment ...
if not exist "%VENV%\Scripts\python.exe" "%PY%" -m venv "%VENV%" || goto :err
set "VPY=%VENV%\Scripts\python.exe"
"%VPY%" -m pip install -q --upgrade pip || goto :err
"%VPY%" -m pip install -q -r requirements.txt pyinstaller-hooks-contrib==2026.8 || goto :err

echo [3/7] PyInstaller bootloader ...
rem  The bootloader is compiled from source with the Microsoft C++ compiler (MSVC): a stock,
rem  precompiled bootloader is what antivirus heuristics most often flag.
rem  Unattended choice: set METEORRX_BUILDTOOLS=install  or  =stock  before running this script.
set "VSWHERE=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe"
set "BT_TRIED="
set "BT_ADD=--add Microsoft.VisualStudio.Workload.VCTools --includeRecommended --passive --norestart"

:bt_detect
set "VSPATH="
if exist "%VSWHERE%" for /f "usebackq delims=" %%i in (`"%VSWHERE%" -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath`) do set "VSPATH=%%i"
if defined VSPATH goto :bl_msvc
if defined BT_TRIED goto :bt_failed
if /i "%METEORRX_BUILDTOOLS%"=="install" goto :bt_install
if /i "%METEORRX_BUILDTOOLS%"=="stock" goto :bl_stock
if defined CI goto :bl_stock
echo.
echo       Visual Studio Build Tools [C++ compiler] are not installed on this PC.
echo       They are needed to compile the PyInstaller bootloader from source, which avoids
echo       most antivirus false positives on MeteorRX.exe.
echo.
echo         1 = Install them now: Microsoft "Build Tools for Visual Studio", free, workload
echo             "Desktop development with C++" only. About 2-3 GB download, 6-8 GB on disk,
echo             15-40 min. Administrator approval [UAC] is requested. Recommended.
echo         2 = Continue with the stock bootloader [higher risk of antivirus false positive]
echo         3 = Stop here
echo.
choice /c 123 /n /m "      Your choice [1, 2 or 3]: "
if errorlevel 3 goto :stopped
if errorlevel 2 goto :bl_stock

:bt_install
set "BT_TRIED=1"
set "VSANY="
if exist "%VSWHERE%" for /f "usebackq delims=" %%i in (`"%VSWHERE%" -latest -products * -property installationPath`) do set "VSANY=%%i"
if not defined VSANY goto :bt_new
echo       Visual Studio is installed without the C++ compiler: adding the C++ workload to
echo       %VSANY%
"%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\setup.exe" modify --installPath "%VSANY%" %BT_ADD%
goto :bt_detect

:bt_new
echo       downloading the official installer from Microsoft [aka.ms/vs/17/release/vs_BuildTools.exe]
curl -fsSL --retry 3 -o "%TEMP%\vs_BuildTools.exe" "https://aka.ms/vs/17/release/vs_BuildTools.exe" || goto :bt_winget
powershell -NoProfile -Command "$s=Get-AuthenticodeSignature '%TEMP%\vs_BuildTools.exe'; Write-Host ('      ' + $s.Status + ' : ' + $s.SignerCertificate.Subject); if($s.Status -ne 'Valid' -or $s.SignerCertificate.Subject -notmatch 'Microsoft Corporation'){exit 1}" || goto :bt_badsig
echo       installing [a progress window opens; approve the administrator prompt]
"%TEMP%\vs_BuildTools.exe" --wait %BT_ADD%
goto :bt_detect

:bt_winget
where winget >nul 2>nul || goto :bt_failed
echo       download failed - trying winget
winget install --id Microsoft.VisualStudio.2022.BuildTools -e --source winget --accept-package-agreements --accept-source-agreements --override "--wait %BT_ADD%"
goto :bt_detect

:bt_badsig
echo       The downloaded installer is not validly signed by Microsoft - not executed.
goto :bt_failed

:bt_failed
echo.
echo       The Build Tools could not be installed, or are not detected yet.
echo       If the installer asked for a restart, restart Windows and run build_exe.bat again.
if defined CI goto :bl_stock
if /i "%METEORRX_BUILDTOOLS%"=="install" goto :err
echo         2 = Continue with the stock bootloader       3 = Stop here
choice /c 23 /n /m "      Your choice [2 or 3]: "
if errorlevel 2 goto :stopped
goto :bl_stock

:bl_msvc
echo       compiling the bootloader from source with MSVC
echo       [%VSPATH%]
call "%VSPATH%\VC\Auxiliary\Build\vcvars64.bat" >nul
set PYINSTALLER_COMPILE_BOOTLOADER=1
"%VPY%" -m pip install -q --force-reinstall --no-deps --no-binary pyinstaller pyinstaller==6.22.3 || goto :bl_msvc_failed
set "PYINSTALLER_COMPILE_BOOTLOADER="
"%VPY%" -m pip install -q pyinstaller==6.22.3 || goto :err
goto :bl_done

:bl_msvc_failed
set "PYINSTALLER_COMPILE_BOOTLOADER="
echo       Compiling the bootloader with MSVC FAILED [see the messages above].
set "BT_TRIED=1"
goto :bt_failed

:bl_stock
echo       using the stock PyInstaller bootloader [antivirus false positives are more likely]
"%VPY%" -m pip install -q --force-reinstall pyinstaller==6.22.3 || goto :err

:bl_done

echo [4/7] Drivers: RTL-SDR Blog v1.3.6 and Zadig 2.9, SHA-256 verified ...
"%VPY%" tools\fetch_drivers.py drivers || goto :err
powershell -NoProfile -Command "$s=Get-AuthenticodeSignature 'drivers\zadig\zadig.exe'; Write-Host ('      zadig.exe ' + $s.Status + ' : ' + $s.SignerCertificate.Subject); if($s.Status -ne 'Valid'){exit 1}" || goto :err

echo [5/7] Building the executables ...
if exist dist\MeteorRX\meteorrx.ini copy /y dist\MeteorRX\meteorrx.ini "%TEMP%\meteorrx.ini.keep" >nul
rem data\ holds calibrations, receiver profiles and the session counter: PyInstaller would delete it
"%VPY%" tools\keep_data.py save || goto :err
"%VPY%" -m PyInstaller --noconfirm --clean --log-level WARN MeteorRX.spec || goto :err
if exist "%TEMP%\meteorrx.ini.keep" move /y "%TEMP%\meteorrx.ini.keep" dist\MeteorRX\meteorrx.ini >nul
"%VPY%" tools\keep_data.py restore || goto :err

echo [6/7] Code signing ...
set "SIGNTOOL="
if defined METEORRX_SIGN_PFX for /f "delims=" %%i in ('dir /b /s "%ProgramFiles(x86)%\Windows Kits\10\bin\*signtool.exe" 2^>nul ^| findstr /i "\\x64\\"') do set "SIGNTOOL=%%i"
if defined METEORRX_SIGN_PFX if not defined SIGNTOOL (echo       signtool.exe not found [Windows SDK]& goto :err)
if defined METEORRX_SIGN_PFX "%SIGNTOOL%" sign /f "%METEORRX_SIGN_PFX%" /p "%METEORRX_SIGN_PASSWORD%" /fd sha256 /tr http://timestamp.digicert.com /td sha256 /d "MeteorRX" dist\MeteorRX\MeteorRX.exe dist\MeteorRX\MeteorRX-cli.exe || goto :err
if not defined METEORRX_SIGN_PFX echo       skipped [no certificate configured]

echo [7/7] Drivers, configuration, self-test of the built program, zip ...
"%VPY%" tools\package_exe.py || goto :err

echo.
echo  Done:  %~dp0dist\MeteorRX\MeteorRX.exe
echo  Give dist\MeteorRX-*-win64.zip to other users: unzip anywhere and run MeteorRX.exe.
echo.
if not defined CI pause
exit /b 0

:stopped
echo.
echo  Stopped at your request - nothing was installed. Run build_exe.bat again at any time.
if not defined CI pause
exit /b 2

:err
echo.
echo  BUILD FAILED - see the messages above.
if not defined CI pause
exit /b 1
