---
name: orderdesk-release
description: Випустити нову версію OrderDesk на прод (8000) через GitHub-реліз і авто-оновлення. Runbook: bump версії → тести → тег → CI → перевірка релізу.
---

# Реліз OrderDesk на прод

Прод (`127.0.0.1:8000`, встановлений `OrderDesk.exe`) оновлюється **сам**, раз на добу перевіряючи GitHub Releases і встановлюючи новіший інсталятор. Твоя робота — зібрати й опублікувати цей інсталятор через тег. Хмара робить решту.

**Ланцюг:** bump версії → тести → commit → tag `vX.Y.Z` → push → CI збирає+smoke-тестить+публікує реліз → 8000 підхоплює.

## Кроки

1. **Підняти версію** (оновлює 4 місця одразу — `__version__.py`, `.iss`, workflow ×6):
   ```
   python bump_version.py X.Y.Z
   ```
   Сам не редагуй версію руками — легко проґавити одне з місць і завалити CI.

2. **Перевірити синхронність + весь код:**
   ```
   .venv/Scripts/python.exe -m pytest tests/test_version_sync.py -q
   .venv/Scripts/python.exe -m pytest -q
   ```
   `test_version_sync` падає, якщо `__version__` розійшовся з `.iss`. Не релізити з червоними тестами.

3. **Закомітити бамп** (окремим комітом, із Co-Authored-By трейлером):
   ```
   git add app/__version__.py installer/OrderDesk.iss .github/workflows/release.yml
   git commit -m "Release X.Y.Z: bump version ..."
   ```

4. **Тег + пуш = тригер релізу** (це незворотна вихідна дія — підтверди в користувача перед пушем):
   ```
   git tag -a vX.Y.Z -m "OrderDesk X.Y.Z — <підсумок>"
   git push origin vX.Y.Z
   ```
   Пуш тега несе й коміти. CI (`.github/workflows/release.yml`) запускається на `tags: v*`.

5. **Дочекатись CI** (windows-latest, ~3 хв: onedir + Inno Setup + до 180с smoke health-check):
   ```
   gh run watch <run-id> --repo kuubek5/order-desk --exit-status
   ```
   Запусти у фоні (`run_in_background`), не блокуй.

6. **Підтвердити реліз:**
   ```
   gh release view vX.Y.Z --repo kuubek5/order-desk --json assets --jq '.assets[].name'
   ```
   Мають бути `OrderDesk-Setup-X.Y.Z.exe` + `.sha256`.

7. **Доставка на 8000:** автоматична, до доби. Прискорити — рестарт встановленого додатку (update-check спрацьовує через ~30с після старту). Рестарт прод-процесу — лише з дозволу користувача.

## Пастки

- Версія зашита в 4 місцях — `bump_version.py` покриває всі; ручний sed колись проґавлював workflow і завалював «Verify installer exists».
- Провал smoke-тесту в CI → реліз НЕ створюється (безпечно, 8000 лишається на старій версії). Читай хвіст логу CI + `startup-error.txt` із артефактів.
- Авто-оновлення має відому пастку (watchdog + frozen-app subprocess) — див. `[[project_orderdesk_autoupdate_trap]]` у пам'яті. Якщо 8000 не оновлюється — інсталятор можна запустити вручну з `%LOCALAPPDATA%\OrderDesk\updates\`.
- Тег незворотний — перед пушем звірити версію й що всі потрібні коміти в HEAD.
