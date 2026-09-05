# Арти для екранів-блокаторів («розділ у розробці / тестується»)

Чотири ілюстрації, згенеровані під KuubMill (05.09.26). **Біле на чорному** —
накладаються як CSS-маска яскравістю (`mask-mode: luminance`) і заливаються
акцентом теми, тому один файл фарбується і teal (база), і amber (Forge).
Ніяких дублів під тему не потрібно.

| Файл | Образ | Напис на екрані | Коли пасує |
|---|---|---|---|
| `mill.jpg` | коронка під шпинделем в іскрах | «Розділ ще фрезерується» | розділ у активній розробці, є прогрес |
| `blueprint.jpg` | креслення коронки в 3 проєкціях, штамп ЧЕРНЕТКА | «На кресленні» | розділ проєктується / затверджується |
| `gauge.jpg` | індикатор на коронці у V-блоці, чек-лист | «Проходить калібрування» | розділ працює, але тестується |
| `shutter.jpg` | ролета опущена, знизу світло, стрічка | «ЗАЧИНЕНО на роботи» | тимчасово зачинено, є час відкриття |

Макап усіх чотирьох (з версткою екрана й банером для адміна):
https://claude.ai/code/artifact/271bd156-2df8-47c0-9ee8-6dc2741553cc

Механізм закриття розділу вже вбудований — `app/services/section_gate.py`
(реєстр `SECTIONS`, стан у налаштуваннях `section_state:<розділ>`),
`app/routers/section_gate.py` (`blocked_response` / `admin_banner`), шаблони
`section_blocked.html` + `_section_admin_banner.html`, стилі `css/blocked.css`.
Зразок підключення — `app/routers/stats.py`. Адмін бачить розділ із банером і
може змінити арт або «Відкрити для всіх» без деплою.

Як накласти арт окремо (мінімум):

```css
.art {
  -webkit-mask-image: url(/static/img/blockers/mill.jpg); mask-image: url(/static/img/blockers/mill.jpg);
  -webkit-mask-mode: luminance; mask-mode: luminance;
  -webkit-mask-size: contain; mask-size: contain; -webkit-mask-repeat: no-repeat; mask-repeat: no-repeat;
  background: radial-gradient(circle at 60% 40%, var(--accent-c), var(--accent) 45%, var(--accent-d));
}
```

Оригінали генерацій (чернетки Lite + фінали Pro 2K) і `.log.md` з промптами —
у `design/kuubmill_blocker-*`. Ці копії — робочі, для застосунку; при заміні
арту оновлювати тут, у `design/` лишається історія.
