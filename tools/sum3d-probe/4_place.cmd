@echo off
chcp 65001 >nul
rem Рівень 2: поставити ОДИН STL на КОПІЮ диска .blk. Перетягни файл STL на цей файл.
rem Оригінали STL і диска лише копіюються; результат лежить у C:\sum3d_probe\<дата>\work.
if "%~1"=="" goto usage
echo УВАГА: Sum3D створить або допише свій журнал importstl.log у своїй теці.
echo Наявних файлів Sum3D це не змінює. Не згоден - просто закрий це вікно.
pause
set "SUMDIR="
echo Тека Sum3D, наприклад C:\iCAMV5smart. Просто Enter - визначити саму.
set /p SUMDIR=Тека:
if "%SUMDIR%"=="" goto auto
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0sum3d_probe.ps1" -AllowRunning -Place -SumDir "%SUMDIR%" -Stl "%~1"
goto done
:auto
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0sum3d_probe.ps1" -AllowRunning -Place -Stl "%~1"
:done
echo.
echo Звіт і готові проєкти лежать у C:\sum3d_probe
pause
goto :eof
:usage
echo Перетягни файл STL мишею на 4_place.cmd
pause
