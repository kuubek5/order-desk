---
target: екран тріажу пошти (Нові з пошти)
total_score: 22
max_score: 40
na_heuristics: 
p0_count: 0
p1_count: 2
timestamp: 2026-08-23T14-12-49Z
slug: app-templates-mail-triage-html
---
Method: dual-agent (A: design review · B: detector + measured evidence). No screenshot — the browser pane returned 0x0 after 2 attempts (known project trap); all measurements derived from rendered HTML + authored CSS.

## Design Health Score — 22/40 (Acceptable)

| # | Heuristic | Score | Key issue |
|---|---|---|---|
| 1 | Visibility of system status | 3 | Strong coverage, but never says WHEN the last check ran — only "кожні 2 хв" |
| 2 | Match system / real world | 3 | Good shop Ukrainian, then a traversal-security sentence parked in the list footer |
| 3 | User control and freedom | 2 | Full accept redirects OUT of triage (/?source=client); partial accept stays. Reject deletes the row with no undo at point of action |
| 4 | Consistency and standards | 1 | One download glyph on 3 different scopes; two reject affordances with opposite confirm contracts; THREE accept affordances; "Архів" twice as different destinations; role=tab without aria-selected |
| 5 | Error prevention | 2 | Accept permitted while N files still sit behind un-fetched links; sticky-bar reject has no confirm |
| 6 | Recognition rather than recall | 2 | 16 list elements carry meaning only in title — including the download-all toggle and every readiness badge |
| 7 | Flexibility and efficiency | 1 | triage_status.py defines "готово" as one-click-ready; the UI charges 4 clicks. No shortcuts, no bulk actions |
| 8 | Aesthetic and minimalist | 2 | 11 font sizes (10/10.5/11/11.5/12/12.5/13/14.5/15.5/17/22) — hand-tuned, not a scale |
| 9 | Error recovery | 3 | Per-link error text, truthful toasts, self-explaining accept refusal |
| 10 | Help and documentation | 3 | Good inline hints, arguably too many (6x .wiz-hint in one pane) |

## Design Specificity Verdict

Skeleton is stock; payload is genuinely dental-lab. Strip four elements and this is an off-the-shelf dark mail client: 380px list + reading pane, pill tabs with counts, teal #14b8a6 on near-black (reads as dev tool, not precision manufacturing).

Authored and non-transferable: the .wiz-pathbox destination preview with exists/new per-segment colouring; "3D-друк?" as a first-class readiness state; material candidate chips; the STL stage.

But the COMPOSITION is not authored for this operator: the three most consequential controls (accept, reject, global download-all) are the same 30-34px tinted ghost button, and the one that creates an order + moves files + writes to Sheets is the SMALLEST of them.

Against the brand's "спокійна сила": three simultaneous infinite pulse animations (beacon 2.6s, unread dot 2s per row, tab badge 2s) = 8 concurrent loops at 6 unread letters, in the peripheral vision of someone who keeps this screen open all day.

Deterministic scan: 0 findings — but materially limited. The 7 templates link no CSS and carry no inline styles, so every colour/type/motion rule got empty input. Read it as "no markup-level anti-patterns", NOT "the screen is clean". The detector was verified working against a synthetic file (returned overused-font + bounce-easing).

## What's Working

1. .wiz-pathbox destination preview — answers the one question the operator cannot answer from memory before an irreversible file move: does this client already have a folder, or am I about to create a near-duplicate? Makes duplicate-folder creation visible rather than a silent side effect.
2. Two-plane depth split — list recessed (--bg-rail, no shadow), detail lifted (--card-2, real shadow). "Where I search" vs "where I work" reads pre-attentively, saving a re-orientation beat on every one of dozens of daily returns.
3. One honest vocabulary for attachment state — pending/skipped/ready surfaces in four coordinated places with identical wording. Given that a missing STL costs a milled unit, coherence here is worth more than polish anywhere else.

## Priority Issues

### [P1] No visual apex — the primary action is the smallest button on screen

.btn.primary is a 15%-alpha tint applied identically to «Перевірити пошту», «Розпакувати архіви», «Скачати файли», «Додати правило» and «Прийняти». .btn.sm then shrinks the accept button to 30px while «Розпакувати архіви» stays 34px. Nothing on the screen is a filled button.

Fix: exactly one filled treatment (background: var(--accent-b); color: var(--bg)) reserved for «Прийняти» and «Прийняти в чергу». Demote the files-pane primaries to neutral. Sticky accept to 36px, min-width 150px.
Command: /impeccable layout

### [P1] Destructive reject 8px from accept with no confirm — plus an Enter double-fire bug

(a) The sticky-bar reject is a plain form with no hx-confirm, 8px from accept, same size and weight — while the LIST reject does confirm. Same screen, same verb, two safety contracts.
(b) FUNCTIONAL BUG: .mailrow carries hx-trigger="click, keyup[key=='Enter']"; the reject form stops only click. Tab to the cross, press Enter and the letter is BOTH rejected AND opened. Space also does nothing on a role="button" row.

Fix: move reject and «У фільтр» out of the sticky bar (disposal, not primary work), or give them the same confirm plus 24px separation and a hairline divider. Stop keyup propagation on the cross. Handle Space on the row.
Command: /impeccable harden

### [P2] Three different actions share one download glyph; three accept affordances

The same SVG sits on «Скачувати все» (a persistent global setting), «Скачати файли» (this letter's MIME parts) and «Скачати всі файли» (this letter's share links). The labels differ by the single word "всі". Separately, three accept affordances coexist (seg-tab, wizard next, sticky accept), two of which do the same thing.

Fix: move the global toggle out of .head-actions (Settings, or a labelled state chip below the tab strip). Distinct glyphs for the two per-letter fetches. Rename so they differ by noun: «Скачати вкладення» / «Скачати за посиланням».
Command: /impeccable clarify

### [P2] Four entry points for "filter this", each with different semantics

Suggest banner creates a sender rule. The teach-filter details creates a keyword rule, retroactively. Sticky «У фільтр» moves this letter only, no rule. The filtered-tab panel creates either kind. The distinction between the middle two is documented only in a Jinja comment and a title attribute.

Fix: collapse to two, named by scope — "remove this letter" and "never show these again" (one dialog offering sender or word, pre-filled from the open letter).
Command: /impeccable distill

### [P2] Accessibility: naming, target size, contrast

Measured: 11 buttons with no accessible name (SVG-only cross, title only); 16 elements carry meaning only in title; the teach-filter input has a placeholder as its only label; the cross is 22x22px (below the WCAG 2.2 AA 24x24 floor); role="tab" without aria-selected/aria-controls; 5 contrast failures all rooted in one token — --ink-3 #7789a0 on --card-2 = 4.37, on --card-3 = 4.00, state pill 4.47, idle seg-tab 4.37 (4.5 required); --line-2 border on --card = 1.43 (3.0 required).

Good news: the readiness badges PASS with margin (10.74 / 7.62 / 5.97); focus-visible rings exist on .mailrow and .seg-tab.

Fix: lift --ink-3 to ~#8698ad and --line-2 to ~#3a4a5f; aria-label on every icon button; cross to 24x24.
Command: /impeccable audit

## Persona Red Flags

Alex (power user, 60+ letters/day): the 15s poll re-renders #mail-list-rows; hx-preserve keeps node identity but not POSITION, so an arriving letter shifts rows mid-click. CLAUDE.md rule 1 forbids exactly this on the handout screen; triage never got the same protection. "готово" promises one click and costs four. Wizard step-2 fields carry hx-trigger="change" targeting the whole #mail-wizard, so tabbing out destroys and recreates the field and focus falls to body (3 fields = 3 focus losses). The 12s sync cooldown is patronising; 3-4s suffices. No j/k, no arrow traversal, no Esc.

Sam (keyboard / screen reader): the Enter double-fire above lands on them. role="tablist" is decorative — no aria-selected, aria-controls, roving tabindex or arrow keys. The panel swap moves no focus and announces nothing (no aria-live). .wiz-progress is aria-hidden with no live region, so step transitions are silent. Critical semantics live only in title.

## Minor Observations

- The sticky bar is not actually sticky. .list-panel has a max-height; .detail-panel has overflow-y:auto and NO height cap. On a long letter the pane grows, the internal scroll never engages, the page scrolls instead, and the bar sticks to the bottom of a card below the fold — failing on exactly the letters that need it. Fix: max-height:calc(100vh - 190px) plus .camtext{max-height:40vh;overflow-y:auto}.
- 11 font sizes, five of them fractional — define 6 steps in tokens.css and snap to them.
- #111823, #1a222e, #1d2734, #3a4a5f hardcoded ~10 times past the token layer (CLAUDE.md section 8 requires one place).
- "Архів" appears twice on screen as two different destinations (mail archive tab, orders archive rail item).
- max-width:1400px wastes ~500px on the target 1920 monitor while the STL stage — the actual verification instrument — is capped at 360px tall.
- The traversal-security sentence in the list footer ships developer reassurance to a CAM operator permanently.
- .filter-rules re-declares its own .btn/.suminput because it is shared with Settings — two button systems now drift independently.

## Questions to Consider

1. If the code defines "готово" as "everything needed to accept in one click", why does the interface charge four clicks and then eject the operator from the screen?
2. Why does the queue screen have a written law against reordering under the operator's hand, while this screen re-renders its list every 15 seconds with no such protection?
3. What does this layout look like on a 40-letter day? Is a scrolling reading-pane inbox the right shape, or is the real job "clear a stack of 40 near-identical decisions" — which wants a dense worklist and an inline STL peek?
