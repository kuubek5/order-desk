@echo off
chcp 65001 >nul
rem Суха перевірка: шукає теки Sum3D і потрібні функції. DLL НЕ завантажується, Sum3D не чіпається.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0sum3d_probe.ps1" -DryRun
echo.
echo Готово. Звіт лежить у C:\sum3d_probe
pause
