@echo off
rem Подвійний клік — інтерактивний генератор ключів активації Order Desk.
rem Використовує локальний .venv, якщо він є, інакше системний python.
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" keygen.py
) else (
    python keygen.py
)
echo.
pause
