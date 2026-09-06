# Frontend review — e52f85f..HEAD (76 commits)

Scope: app/templates/**, app/static/js/**, app/static/css/**. All findings verified by
reading the actual current file content (not just the diff) unless marked "unverified".

## Method / clean checks (no findings — listed so they aren't re-checked)
- `node --check` on every file in app/static/js: all pass, no syntax errors.
- Every `hx-get`/`hx-post` target in app/templates (cross-checked ~100 unique paths,
  including param'd ones like `/orders/{{ order.id }}/status`) resolves to an entry in
  tests/route_inventory.txt.
- Every `href="/..."` in app/templates (non-static, non-hash) resolves to a GET route in
  tests/route_inventory.txt.
- Every CSS file on disk (app/static/css/*.css, 31 files) is linked from either
  base.html or some template's `{% block extra_head %}`; no orphaned/unlinked CSS file.
- Polling regions (`hx-trigger="every Ns"`) were checked against embedded forms/inputs/
  open `<details>`: furnaces.html / machines.html gate the poll with
  `[!document.querySelector('#...cards details[open]')]`; `_shift_board.html` gates with
  `[!document.querySelector('#shift-board .sh-edit[open]')]`; `_mail_triage_list.html`
  uses `hx-preserve` per row plus a frozen `since` watermark; `handout.html` only polls a
  cheap hidden `/handout/pulse` span and re-fetches `#handout-list` on a server
  `HX-Trigger` event, not blind polling. No state-loss risk found in the diff.

## HIGH

1. `app/templates/_topbar_nav.html:119-206` + `app/static/css/rail.css:444-447` — When
   the rail is collapsed (`body.rail-collapsed`), `.rail-label` gets `display:none`,
   which removes the only accessible name (icon is `aria-hidden="true"`, no `aria-label`
   or `title` on any of the ~9 `.rail-nav-item` `<a>` links: Черга, Нові з пошти,
   Ранкова видача, Зміна, Пічки, Верстати, Клієнти, Архів, Пошук, Журнал дій, Журнал
   синку, Статистика, Виробіток, Вхідні, Налаштування). `app.js:823-854` shows a visual
   hover/focus tooltip reading `.rail-label` textContent, but it is a floating `<div
   role="tooltip">` not wired via `aria-describedby`/`aria-labelledby` to the link, so it
   helps sighted users only — a screen-reader user tabbing through the collapsed rail
   hears nothing for every primary nav item. Fix: add `aria-label="{{ label text }}"`
   directly on each `.rail-nav-item`, independent of the collapsed-state tooltip.

2. `app/static/css/settings_stand.css:94,101-102,218,220,228,260,261,265,266` — hardcoded
   `rgba(255, 107, 82, …)` used for the alarm/error state, but `tokens.css` (via
   `settings.css:126`) defines `--alarm-rgb: 248, 113, 113` — a *different* red — and the
   SAME file already correctly uses `rgba(var(--alarm-rgb), …)` 9 times elsewhere (e.g.
   line 19-ish, `.stand-state-alarm { color: var(--alarm); … }` mixes the token color
   for `color` with the hardcoded rgb for `border-color`/`background` on line 94). This
   is real token drift: the furnace/machine "stand" widget's alarm glow/border no longer
   matches the design system's alarm color. Fix: replace the 8 literal
   `rgba(255, 107, 82, …)` occurrences with `rgba(var(--alarm-rgb), …)`.

## MEDIUM

3. `app/templates/_handout_qc.html:5-6` (`role="dialog" aria-modal="true"`) +
   `app/static/js/handout.js:210-270` — Escape closes the QC checklist dialog
   (`handout.js:267-270`) and it does move focus to the first checkbox on open
   (`qcOpen`, line 226), but there is no focus trap: Tab/Shift+Tab from the last/first
   focusable element inside the dialog moves focus out into the page behind it while the
   dialog is still open and `aria-modal="true"` is claiming otherwise. This is the
   handout screen's main "звір перед видачею" gate, used many times a day. Fix: trap
   Tab within `#qc-dialog` (cycle between first/last focusable) while open, or migrate to
   `<dialog>`.

4. `app/static/css/settings.css` — 54 hardcoded colors added in this diff outside
   tokens.css, several with no `var()` involvement at all (not just a fallback):
   `:666 .matlib-summary .tnum.is-warn{color:#f0a24b}` (should be `var(--warn)`-derived),
   `:696 background-color:#0d1117`, `:819 background:#0c1015 …`, `:825
   background-color:#100d0a`, `:993 background:#0c1015`. `:1013` additionally hardcodes
   *fallback* values that drift from the real tokens: `var(--control-edge,#5f7490)` and
   `var(--accent,#f0b99a)` — harmless while the custom properties are defined, but a
   silent trap if either token is ever renamed/removed (fallback kicks in with a color
   nobody chose). Fix: route these through existing tokens (`--warn`, `--bg`, `--card`,
   etc.) instead of literal hex.

## LOW

5. `app/static/css/settings.css:974` vs `:1019` — `.setv2 .fu-add .fu-add-row` is
   defined twice with different `grid-template-columns` (6 columns at :974 vs 7 columns,
   an extra trailing `auto`, at :1019). Same specificity, later rule wins in the
   cascade, so the :974 declaration is dead code — looks like a column was added later
   without removing the original rule. Harmless today but confusing to future edits.

6. `app/static/css/settings_stand.css:92,159` — `var(--ok, #a8e08a)` used as the
   fallback for the "ok" state color, but the actual `--ok` token (`settings.css:129`)
   is `#5eead4` (teal), a completely different hue from the fallback (`#a8e08a`,
   olive-green). The fallback never fires in practice (the token is always defined via
   tokens.css/settings.css), so this is inert dead code rather than a live color bug —
   but if someone ever forgets to load tokens for a partial render, the color would jump
   to a hue that doesn't exist anywhere else in the palette. Fix: drop the stale
   fallback or match it to the real token.

7. `app/templates/account.html:290-291` — `<span class="btn sm apex">Взяти</span>` /
   `<span class="btn sm">Файли</span>` are static preview swatches inside the
   appearance-settings mock queue row (not real controls — no `hx-*`, no `href`, no
   `type`). Verified as intentional (a `for`-loop styleguide preview a few lines above),
   not a real interactive element missing semantics — noting only because it initially
   looks like a button-styled non-interactive element, a common a11y anti-pattern
   elsewhere; here it's inert decoration and not a bug.

## Notes on things checked and found OK (not findings, for completeness)
- `.btn.apex` (the single-filled-button-per-screen rule from CLAUDE.md §14) is used
  consistently: one apex button per screen in `_mail_wizard.html`, `_shift_board.html`
  (view), `_shift_card.html` (view), `shift.html` (pin), `queue.html` (add-work),
  `_matlib_detail.html`/`settings_materials.html` use a separate `.ml-btn-apex` class
  for the same single-filled-button role in the materials screens — consistent, no
  double-apex screen found.
- `_queue_views.html` (new saved-views widget) is a good a11y example: every icon-only
  button has both `title` and `aria-label`, inputs have `aria-label`, focus-visible
  styles defined in `v2a_queue.css`.
- `palette.js` (Ctrl+K) closes on Escape (both input-level and document-level handlers)
  and on outside click; no focus-trap bug found there.
- `app/static/js/stl-render-core.js` (`window.StlRenderCore`) is consumed by both
  `stl-preview.js` and `stl-gallery.js` — not dead code despite being a new shared file.
- No CSS files linked in base.html/templates are missing from disk; no CSS file on disk
  is unlinked from every template.
