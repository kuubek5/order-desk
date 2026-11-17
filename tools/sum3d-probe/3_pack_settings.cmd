@echo off
chcp 65001 >nul
rem Збирає налаштування Sum3D в один zip: стратегії, архів фрез, словники виробника.
rem Лише читає. Моделі, проєкти, ключі й ліцензію не бере. Sum3D може бути відкрита.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0sum3d_pack.ps1"
echo.
echo Архів лежить у C:\sum3d_pack
pause
