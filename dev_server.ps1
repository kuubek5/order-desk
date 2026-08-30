# Запускає KuubMill у dev-режимі з live reload.
#
# Порт 8002 (не 8000) навмисно - щоб не конфліктувати з реальним встановленим
# OrderDesk.exe, якщо той теж запущений. Дані тут - окрема dev-база
# (order_desk.db у корені проекту), НЕ реальні дані з лабораторії.
#
# Як користуватись:
#   1. Запусти цей файл (правий клік -> Run with PowerShell, або з терміналу).
#   2. Відкриється браузер на http://127.0.0.1:8002
#   3. Коли Claude змінює код - сервер сам перезапускається за 1-2 секунди.
#      Просто онови сторінку (F5), щоб побачити зміну.
#   4. Щоб зупинити - закрий це вікно або Ctrl+C.

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $projectRoot

$port = 8002
$url = "http://127.0.0.1:$port"

Write-Host "KuubMill (dev, live reload) -> $url"
Write-Host "Дані: локальна dev-база (order_desk.db), не реальні дані лабораторії."
Write-Host "Зупинити: Ctrl+C у цьому вікні."
Write-Host ""

Start-Process $url

& ".venv\Scripts\python.exe" -m uvicorn app.web:app --reload --port $port