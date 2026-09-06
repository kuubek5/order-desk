# Senior frontend review — full UI (not a diff)

Scope: 103 templates (`app/templates/`), 22 JS files / 6.4k lines (`app/static/js/`),
32 CSS files / 13.5k lines (`app/static/css/`), `base.html` link/script order.
Read: all JS in full, `base.html`, `tokens.css`, `theme-forge.css`, all CSS files
grepped for the checklist items, templates sampled + grepped for patterns.

Overall: this is an unusually disciplined codebase for a no-build-step
Jinja+HTMX app — most of the classic anti-patterns this checklist hunts for
are already fixed and documented in code comments (poll-safe scroll, HTMX
swap survival, KMStore, shared STL render core). Findings below are real but
mostly MEDIUM/LOW; only one HIGH.

## HIGH

1. **`app/static/css/furnaces.css:181-201` + `app/static/css/v2a_queue.css:739-742,836-839` — material accent colors hardcoded as raw hex, duplicated in two files, not tokenized.** — VERIFIED.
   `#8fbfe0` (zr), `#d8b46a` (pmma), `#63c88b` (ti), `#9aa7b4` (slm), `#3fb6c9` (wax)
   appear as literals in both files (4 places for `mat-ti` alone). This is the
   material-identity signal CLAUDE.md §14 calls a load-bearing channel ("край —
   матеріал"), yet it's the one color system in the app that never went through
   the tokens.css consolidation the 30.08.26 alias-token pass did for every
   other semantic color (`--alarm`, `--warn`, `--ok`, `--email`...). Fix:
   add `--mat-zr/--mat-pmma/--mat-ti/--mat-slm/--mat-wax` to `tokens.css` and
   point both files at them — even if the values stay constant across themes,
   single-sourcing avoids the two files drifting on the next color tweak.

## MEDIUM

2. **`app/static/js/palette.js` and `app/static/js/settings_console.js` — two independent command-palette implementations (~150 lines each: build/render/search/keyboard-nav).** — VERIFIED, intentional per comment (`settingsPaletteOwnsHotkey()`, well-documented hand-off), but the UI/keyboard code itself (list render, arrow-key nav, escape handling) is duplicated rather than shared. Also duplicates an `esc()`/`escapeHtml()` HTML-escaper (`palette.js:58`, `settings_console.js:425`). Fix: extract a shared `KMPaletteList` helper (render+nav+escape) used by both.
3. **No z-index scale/tokens — 22 distinct raw values from -2 to 1300 across 15 files.** — VERIFIED (`furnaces.css`, `login.css`, `settings.css`, `v2a_queue.css`, `screens.css`, `feedback.css`, `update_overlay.css` all pick their own numbers: 0,1,2,3,4,5,6,20,30,40,60,70,80,100,200,210,880,890,960,1000,1200,1300). Works today only because layering was worked out by trial; a new overlay has no scale to consult and will guess. Fix: a `--z-*` ladder in `tokens.css` (dropdown/sticky/modal/toast/update-overlay tiers).
4. **4 different main-container max-widths, no shared token** — VERIFIED: `base.css:189` generic `main{max-width:1400px}`, `v2a_queue.css:17` `main.q2{max-width:none}`, `v2a_screens.css:14` `main.worldv2{max-width:1000px}`, `settings_console.css:34` `main.setv2.scon{max-width:1080px}`. Each screen re-decided independently; nothing says whether 1000 vs 1080 is deliberate or drift.
5. **`hx-indicator` on only 21 of 117 `hx-*` verb attributes (~18%)** — VERIFIED by count, UNVERIFIED whether the remaining 82% have equivalent feedback via toast/`htmx:afterSwap`/disabled-button patterns (many do, per code read — e.g. queue sync sweep, mail-sync button) but that wasn't checked action-by-action. Given CLAUDE.md's own rule ("мовчазна кнопка = не готово"), worth a pass to confirm every mutating action has *some* visible feedback, not just the ones that happen to show a toast.
6. **`_mail_detail_panel.html` rendered from 7 call sites in `app/routers/mail.py`.** — VERIFIED OK on inspection: all 7 funnel through one `_mail_panel_context()` builder, so no context drift risk today — but flagging because it's the single highest-fan-in fragment in the app; a future call site that builds context ad hoc (bypassing the helper) would silently break only in the panel's less-visited tabs.
7. **No `@media print` anywhere in 32 CSS files.** — VERIFIED. Low risk for a shop-floor tool, but the archive/passport screens (`order_detail.html`, "Хроніка") read as things someone might eventually want on paper (QC records, brak history).

## LOW

8. **182 inline `<svg><path>` blocks in templates, heavy repetition** (`M20 6 9 17l-5-5` check ×14, `M12 9v4`/`M12 17h.01` warning-dot ×10 each, folder icon ×8, chevron ×7). Top files: `_topbar_nav.html` (22 inline SVGs), `_mail_detail_panel.html` (18), `queue.html` (14), `_order_row.html` (11), `_handout_cards.html` (11). Sprite/`<use>` candidate — see Architecture notes.
9. **`!important` — only 8 uses total, in 3 files** (`update_overlay.css` 5, `settings_console.css` 2, `v2a_queue.css` 1). Not a problem at this count, noted only because the checklist asked; no action needed.
10. **CSS class-name overlap between cascade-layer pairs** (`queue.css`↔`queue_table.css` 18 shared selectors, `v2a_queue.css`↔`queue.css` 16, `screens.css`↔`v2a_screens.css` 8). VERIFIED OK — this is the documented `base.css → v2a_*.css` override cascade (base.html comments call it out explicitly, link order is the mechanism), not accidental duplication. Long-term cleanup candidate once v2a fully retires the pre-v2a rules (see Architecture notes).
11. **`app/static/js/lookgear.js`** attaches its own `document` click/keydown "close panel on outside click / Esc" listener once per `[data-look-gear]` instance (`initGear` runs per-instance, line 328/332). With 2 gear instances on the page (queue + mail share the component but only one is ever mounted per page) this is currently harmless, but if a future screen ever mounts two gears on one page it'll double-fire. Low priority, just noting the pattern isn't instance-scoped.

## VERIFIED OK (checked, no issue found)

- **XSS / `|safe` audit**: 3 uses of `|safe` in templates (`account.html:281` icon path from a fixed dict, `mail_triage.html:131` a Jinja-rendered sub-template re-injected — already escaped internally, `section_blocked.html:36` a static copy dict from `section_gate.py`). No user-controlled data reaches `|safe` or `innerHTML` unescaped anywhere checked. `app.js` toast builder uses `textContent` for all dynamic text; only static icon markup goes through `innerHTML`.
- **localStorage**: fully unified through `KMStore` (`storage.js`) — versioned key prefix, private-mode-safe, one-time legacy-key migration. No file writes `localStorage` directly.
- **Toast/notification system**: single implementation (`app.js:showToast`), reused everywhere via `window.showToast` and the server-driven `HX-Trigger: {"toast": ...}` convention. Not duplicated.
- **STL preview code**: `stl-preview.js` (queue/handout) and `stl-gallery.js` (mail triage) share a real common core (`stl-render-core.js`) — WebGL context, geometry fetch/cache, mesh centering, disposal — explicitly built to kill a prior duplication (documented in the file header). Good example to point to.
- **Poll-vs-edit-in-progress protection**: queue 15s poll (`queue.js`) and mail 15s poll (`mail.js`) both skip the swap when an input inside the target has focus, both preserve inner scroll (`.tablewrap`/`.listwrap`) across swaps, both skip on byte-identical response. Consistently implemented, not just present on one screen.
- **Event listeners across HTMX swaps**: virtually everything is delegated on `document`/`document.body` rather than bound to swapped nodes — checked `queue.js`, `mail.js`, `handout.js`, `widgetedit.js`, `lookgear.js`, `app.js`; the few `htmx:afterSwap`/`afterSettle` re-decoration handlers found (spotlight tilt, draggable flags, aria-expanded sync) are all cheap (`querySelectorAll` + class toggle) and re-run only on the swap of their own container, not globally.
- **Contrast**: `--color-text-muted` (#8fa3bb) on `--color-bg` (#0d1117) computes to ≈7.3:1 (AAA). `tokens.css` comments show the team already measured and fixed `--ink-3` for 4.5:1 on the lightest surface (`--card-3`) rather than just the darkest — a real prior contrast audit, not luck.
- **`hx-confirm` copy**: all 8 uses follow the same pattern (question + consequence, e.g. "Видалити цю роботу з черги? Її буде переміщено в «Архів»..."), consistent tone across routers.
- **ES5 "machine agent" page**: no such page exists under `app/templates`/`app/static` — the machine agent (imes-icore telemetry) is a headless Python service in `agent/` with no web assets. The ES-level requirement doesn't apply to anything in this review's scope; all page JS is modern (arrow functions, `const`/`let`, template literals, `AbortController`) which matches CLAUDE.md's "modern Chromium operator PC" target.
- **Reduced-motion**: 20 of 32 CSS files have `@media (prefers-reduced-motion: reduce)` rules; the animated screens checked in JS (queue row-rising FLIP, login dynamic quotes, spotlight tilt, settings counters) all gate on `matchMedia("(prefers-reduced-motion: reduce)")` before starting.
- **Dead CSS/JS files**: none found. Every one of the 32 CSS files and 22 JS files is referenced from at least one template's `extra_head`/`extra_scripts` block or `base.html`.
- **Route/template inventory guard**: `tests/test_route_inventory.py` / `tests/test_frontend_assets.py` already catch several of this checklist's categories (JS syntax, script order, template existence) — reviewed their scope, they're real and not stale.

## Architecture notes (≤12 lines)

- **Consolidate**: material colors → `tokens.css` (`--mat-*`, finding 1); the two command-palette implementations → one shared list/nav helper (finding 2); a `--z-*` scale (finding 3) and a single main-container width token consumed by `base.css`/`v2a_*.css`/`settings_console.css` (finding 4).
- **Sprite plan**: the ~15 icons that repeat 5+ times (check, chevron, folder, warning-triangle, X, eye) are good `<svg><symbol>` candidates in one `_icons.html` include with `<use href="#ic-check">`; biggest wins are `_topbar_nav.html`, `_mail_detail_panel.html`, `queue.html`.
- **CSS file map**: `tokens.css` (source of truth) → `fonts.css` → `base.css` (legacy component styles) → `rail.css`/`queue_table.css`/`queue.css`/`order_detail.css`/`screens.css` (pre-v2a screens) → `v2a_*.css` (redesign layer, overrides the previous group by cascade order, not specificity — order in `base.html` IS the contract) → `treatment-a.css`/`theme-forge.css` (theme overlays) → `icon-styles.css`/`element-styles.css`/`lookgear.css`/`feedback.css` (isolated components). Once v2a fully replaces the pre-v2a screens, the base/v2a pair-files (finding 10) can merge and drop ~34 duplicate selectors.
- **Per-screen CSS** (`stats.css`, `account.css`, `blocked.css`, `clients.css`, `shift.css`, `matlib.css`, `vyrobitok.css`, `settings_stand.css`, `login.css`, `furnaces.css`, `settings_console.css`, `settings.css`) load via each template's own `extra_head` — correctly scoped, not dead weight on other screens.
