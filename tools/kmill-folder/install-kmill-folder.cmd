@echo off
rem Ставить помічник відкриття тек KuubMill на ЦЬОМУ ПК оператора.
rem Реєструє протокол kmill-folder:// (лише для поточного користувача, HKCU —
rem прав адміністратора НЕ треба), щоб кнопка «Відкрити папку» в KuubMill
rem відкривала мережеву теку в Провіднику саме на цьому ПК.
rem Запуск: подвійний клік. Повторний запуск безпечний (перезаписує).
setlocal
chcp 65001 >nul

set "DIR=%LOCALAPPDATA%\KuubMill"
if not exist "%DIR%" mkdir "%DIR%"

copy /y "%~dp0open-folder.vbs" "%DIR%\open-folder.vbs" >nul
if errorlevel 1 (
  echo [ПОМИЛКА] Не вдалося скопіювати open-folder.vbs поруч із цим файлом.
  pause
  exit /b 1
)

reg add "HKCU\Software\Classes\kmill-folder" /ve /d "URL:KuubMill Folder" /f >nul
reg add "HKCU\Software\Classes\kmill-folder" /v "URL Protocol" /d "" /f >nul
reg add "HKCU\Software\Classes\kmill-folder\shell\open\command" /ve /d "wscript.exe \"%DIR%\open-folder.vbs\" \"%%1\"" /f >nul

echo.
echo Готово. Помічник відкриття тек встановлено для цього користувача.
echo Тепер кнопка «Відкрити папку» в KuubMill відкриватиме теку в Провіднику.
echo (Якщо KuubMill відкрито в браузері — перезавантаж сторінку.)
echo.
pause
