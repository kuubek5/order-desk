@echo off
chcp 65001 >nul
rem Проба лише на читання. Перетягни файл .cam на цей файл.
rem Працює з КОПІЄЮ проєкту; вікна Sum3D можуть лишатись відкритими.
if "%~1"=="" goto usage
set "SUMDIR="
echo Тека Sum3D, наприклад C:\iCAMV5smart. Просто Enter - визначити саму.
set /p SUMDIR=Тека:
if "%SUMDIR%"=="" goto auto
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0sum3d_probe.ps1" -AllowRunning -SumDir "%SUMDIR%" -Cam "%~1"
goto done
:auto
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0sum3d_probe.ps1" -AllowRunning -Cam "%~1"
:done
echo.
echo Звіт лежить у C:\sum3d_probe
pause
goto :eof
:usage
echo Перетягни файл проєкту .cam мишею на 2_probe.cmd
pause
