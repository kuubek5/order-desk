// Черга робіт — головний екран (CLAUDE.md §9, екран 1).
//
// Найбільший файл не випадково: тут полл кожні 15с, вбудоване редагування,
// форма додавання, режим редагування макета й слайдовер паспорта роботи.
//
// Наскрізне правило екрана: полл НЕ МАЄ смикати оператора. Тому і пропуск
// запиту, коли фокус у полі, і збереження скролу (у вікні И всередині
// .tablewrap), і пропуск підміни, коли відповідь байт-у-байт та сама.

// Queue auto-refresh guard. #queue-rows polls GET /?…&partial=rows every 15s
// (see _queue_rows.html) so sheet edits show without an F5. But a blind swap
// would wipe a comment/Sum3D an operator is mid-typing — so skip the poll's
// request whenever the focus is inside a queue field being edited. The element
// isn't replaced, so its own 15s timer just tries again next tick, once the
// operator has moved on.
document.addEventListener("htmx:beforeRequest", (event) => {
  const poller = event.target;
  if (!poller || poller.id !== "queue-rows") return;
  const active = document.activeElement;
  if (
    active &&
    poller.contains(active) &&
    active.matches("input, textarea, select")
  ) {
    event.preventDefault();
  }
});

// Poll swap without losing the operator's place. Replacing the whole
// #queue-rows node (hx-swap=outerHTML) makes the document momentarily shorter,
// so the browser clamped window scroll to the top on every poll tick — an
// operator reading the bottom of the queue got yanked up every 5-15s.
// Two layers:
//   1. If the poll response is byte-identical to the last one, skip the swap
//      entirely (no DOM churn, no flicker) — the common case, nothing changed.
//   2. When content DID change, remember the scroll position and restore it
//      right after the swap settles.
let lastRowsResponse = null;

let savedScrollY = null;

// The queue table scrolls INSIDE .tablewrap (overflow:auto, capped height), not
// the window — so when the poll replaces #queue-rows (which contains .tablewrap)
// the new container starts at scrollTop 0 and the operator reading the bottom
// gets yanked up. Restoring only window.scrollY missed this (the window barely
// scrolls). Save and restore the inner scroll too.
let savedTableScroll = null;

document.addEventListener("htmx:beforeSwap", (event) => {
  const target = event.detail.target;
  if (!target || target.id !== "queue-rows") return;
  const incoming = event.detail.serverResponse;
  if (incoming != null && incoming === lastRowsResponse) {
    event.detail.shouldSwap = false;
    return;
  }
  lastRowsResponse = incoming;
  savedScrollY = window.scrollY;
  const wrap = target.querySelector(".tablewrap");
  savedTableScroll = wrap ? wrap.scrollTop : null;
});

document.addEventListener("htmx:afterSettle", (event) => {
  if (savedScrollY == null && savedTableScroll == null) return;
  const el = event.detail && event.detail.elt;
  if (el && el.id === "queue-rows") {
    if (savedScrollY != null) window.scrollTo(0, savedScrollY);
    if (savedTableScroll != null) {
      const wrap = el.querySelector(".tablewrap");
      if (wrap) wrap.scrollTop = savedTableScroll;
    }
    savedScrollY = null;
    savedTableScroll = null;
  }
});

// Inline comment textarea: grow to fit the full text while focused/typing so a
// long technician comment is readable and editable, collapse back to one line
// on blur. Enter saves (blurs → the form's hx-trigger=change fires), Shift+Enter
// inserts a newline. All delegated on document so it survives the 15s poll swap.
function growComment(el) {
  // Force wrapping here (not only via CSS :focus) so the height math is
  // reliable even before :focus paints, then size to the wrapped content.
  el.style.whiteSpace = "pre-wrap";
  el.style.height = "auto";
  el.style.height = el.scrollHeight + "px";
}

function collapseComment(el) {
  el.style.whiteSpace = "";
  el.style.height = "";
}

document.addEventListener("focusin", (event) => {
  const ta = event.target.closest && event.target.closest(".cam-comment-input");
  if (ta) growComment(ta);
});

document.addEventListener("input", (event) => {
  const ta = event.target.closest && event.target.closest(".cam-comment-input");
  if (ta) growComment(ta);
});

document.addEventListener("focusout", (event) => {
  const ta = event.target.closest && event.target.closest(".cam-comment-input");
  if (ta) collapseComment(ta);
});

document.addEventListener("keydown", (event) => {
  if (event.key !== "Enter" || event.shiftKey) return;
  const ta = event.target.closest && event.target.closest(".cam-comment-input");
  if (!ta) return;
  event.preventDefault();
  ta.blur(); // change event → hx-post saves
});

// Enter anywhere in the "add work" form submits it. Relying on the browser's
// implicit submission is not enough here: the form's onsubmit disables the
// submit button, and a form whose default button is disabled swallows Enter
// silently — the operator types, presses Enter, and nothing happens.
// requestSubmit() (not submit()) keeps onsubmit and native validation running.
document.addEventListener("keydown", (event) => {
  if (event.key !== "Enter" || event.shiftKey) return;
  const target = event.target;
  if (!target || !target.closest) return;
  const field = target.closest("[data-addwork] input");
  if (!field) return;
  const form = field.closest("[data-addwork]");
  if (!form) return;
  event.preventDefault();
  const submit = form.querySelector("[data-addwork-submit], button[type=submit]");
  if (submit && submit.disabled) return; // a submit is already in flight
  if (typeof form.requestSubmit === "function") form.requestSubmit();
  else form.submit();
});

// Inline "add work" form in the queue card-head: the "+" toggle reveals the
// client-work fields right above the rows; cancel hides them. Delegated so it
// survives HTMX swaps.
// Apply the Клієнт/Лабораторія mode across ALL field-rows of the form. Hidden
// fields are also DISABLED so the inactive mode's values aren't posted and a
// hidden input never blocks submit.
function applyAddworkType(form, type) {
  form.querySelector("[data-addwork-typeinput]").value = type;
  form.querySelectorAll("[data-addwork-type]").forEach((b) =>
    b.classList.toggle("is-active", b.dataset.addworkType === type)
  );
  form.querySelectorAll("[data-addwork-client]").forEach((el) => {
    el.hidden = type !== "client";
    el.disabled = type !== "client";
  });
  form.querySelectorAll("[data-addwork-lab]").forEach((el) => {
    el.hidden = type !== "lab";
    el.disabled = type !== "lab";
  });
}

// Show the per-row remove "✕" only when there's more than one row.
function refreshAddworkRows(form) {
  const rows = form.querySelectorAll("[data-addwork-row]");
  rows.forEach((row) => {
    const rm = row.querySelector("[data-addwork-removerow]");
    if (rm) rm.hidden = rows.length <= 1;
  });
}

document.addEventListener("click", (event) => {
  const toggle = event.target.closest("[data-addwork-toggle]");
  if (toggle) {
    const form = document.querySelector("[data-addwork]");
    if (!form) return;
    const open = form.hidden;
    form.hidden = !open;
    toggle.setAttribute("aria-expanded", String(open));
    if (open) {
      const first = form.querySelector("input:not([disabled])");
      if (first) first.focus();
    }
    return;
  }
  if (event.target.closest("[data-addwork-cancel]")) {
    const form = document.querySelector("[data-addwork]");
    if (form) {
      form.hidden = true;
      const t = document.querySelector("[data-addwork-toggle]");
      if (t) t.setAttribute("aria-expanded", "false");
    }
    return;
  }
  // "+ ще рядок" — clone the last field-row, clear its values, keep current mode.
  const addRow = event.target.closest("[data-addwork-addrow]");
  if (addRow) {
    const form = addRow.closest("[data-addwork]");
    const rowsBox = form.querySelector("[data-addwork-rows]");
    const rows = rowsBox.querySelectorAll("[data-addwork-row]");
    const clone = rows[rows.length - 1].cloneNode(true);
    clone.querySelectorAll("input").forEach((el) => { el.value = ""; });
    rowsBox.appendChild(clone);
    const type = form.querySelector("[data-addwork-typeinput]").value;
    applyAddworkType(form, type);
    refreshAddworkRows(form);
    const focusEl = clone.querySelector("input:not([disabled])");
    if (focusEl) focusEl.focus();
    return;
  }
  // Per-row "✕" — remove that row (never the last remaining one).
  const removeRow = event.target.closest("[data-addwork-removerow]");
  if (removeRow) {
    const form = removeRow.closest("[data-addwork]");
    const row = removeRow.closest("[data-addwork-row]");
    if (form.querySelectorAll("[data-addwork-row]").length > 1) {
      row.remove();
      refreshAddworkRows(form);
    }
    return;
  }
  // Settings: "Змінити"/"Вставити" reveals a collapsed credential textarea
  // (JSON keys stay hidden behind a compact status row until the admin
  // actually wants to change them).
  const credToggle = event.target.closest("[data-cred-toggle]");
  if (credToggle) {
    const group = credToggle.closest("[data-cred-toggle-group]");
    const field = group.parentElement.querySelector("[data-cred-field]");
    const opening = field.hidden; // currently collapsed -> this click reveals it
    if (opening && !credToggle.dataset.originalLabel) {
      credToggle.dataset.originalLabel = credToggle.textContent;
    }
    field.hidden = !opening;
    credToggle.textContent = opening ? "Скасувати" : (credToggle.dataset.originalLabel || "Змінити");
    if (opening) {
      const ta = field.querySelector("textarea, input");
      if (ta) ta.focus();
    }
    return;
  }

  // Settings: Сервісний акаунт / Google-акаунт auth-mode toggle.
  const authBtn = event.target.closest("[data-authmode]");
  if (authBtn) {
    const seg = authBtn.closest("[data-authmode-seg]");
    const form = seg.closest("form");
    const mode = authBtn.dataset.authmode;
    form.querySelector("[data-authmode-input]").value = mode;
    seg.querySelectorAll("[data-authmode]").forEach((b) =>
      b.classList.toggle("is-active", b === authBtn)
    );
    form.querySelectorAll("[data-authmode-block]").forEach((el) => {
      el.hidden = el.dataset.authmodeBlock !== mode;
    });
    return;
  }

  // Клієнт / Лабораторія type switch.
  const typeBtn = event.target.closest("[data-addwork-type]");
  if (typeBtn) {
    const form = typeBtn.closest("[data-addwork]");
    const type = typeBtn.dataset.addworkType;
    applyAddworkType(form, type);
    const focusEl = form.querySelector(
      type === "lab" ? '[name="work_order_no"]:not([disabled])' : '[name="client_name"]:not([disabled])'
    );
    if (focusEl) focusEl.focus();
  }
});

// Double-click a queue row's "Шлях" (job_code) cell to open its resolved
// export folder — single click keeps copying job_code to the clipboard for
// pasting into Sum3D (CLAUDE.md screen 1, deliberate "level 1" design, left
// unchanged above). Only wired when a real folder was resolved server-side
// (`data-folder-uri`, see app/templates/_order_row.html); otherwise this is
// a no-op, same "client resolves file:// links" approach handout.html's
// folder links already use.
document.addEventListener("dblclick", (event) => {
  const button = event.target.closest("[data-folder-uri]");
  if (!button) return;

  // A browser blocks a file:// link opened from an http page, so prefer the
  // authenticated loopback-only server route when the element carries an STL
  // preview token (the job_code cell does). Fall back to the file:// URI only
  // where there's no token (kept for any legacy folder link).
  const token = button.dataset.stlPreviewToken || "";
  if (token) {
    fetch("/open-folder", {
      method: "POST",
      body: new URLSearchParams({ token: token }),
      credentials: "same-origin",
    }).catch(() => {});
    return;
  }

  const uri = button.dataset.folderUri || "";
  if (!uri) return;
  window.location.href = uri;
});

// Collapsible section headers ("Лабораторні роботи" / "Роботи з пошти",
// queue.html): is-collapsed class + aria-expanded + CSS grid-template-rows
// transition (see .queue-group-* rules). Both start expanded, so first click
// only ever removes is-collapsed.
document.addEventListener("click", (event) => {
  const toggle = event.target.closest("[data-queue-group-toggle]");
  if (!toggle) return;

  const section = toggle.closest(".queue-group");
  if (!section) return;

  const collapsed = section.classList.toggle("is-collapsed");
  toggle.setAttribute("aria-expanded", String(!collapsed));
});

// v2a sync sweep (queue.html .queue-panel > .sweep). When the operator kicks
// off a Google Sheets sync, the neon strip sweeps across the queue for as long
// as the real POST /sheets/sync is in flight, then the page reloads so the
// session sync_flash and refreshed queue show. Any form posting to /sheets/sync
// (statusline sl-sync button, empty-state button) is intercepted; without JS
// the plain form submit still works (graceful fallback, just no sweep).
document.addEventListener("submit", (event) => {
  const form = event.target.closest('form[action="/sheets/sync"]');
  if (!form) return;
  event.preventDefault();

  const panel = document.querySelector(".queue-panel");
  const button = form.querySelector('button[type="submit"]');
  if (button) button.disabled = true;
  if (panel) panel.classList.add("sweeping");

  // Let the strip complete at least one pass even if the server answers fast.
  const minSweep = new Promise((resolve) => window.setTimeout(resolve, 980));
  const request = fetch("/sheets/sync", {
    method: "POST",
    headers: { "X-Requested-With": "fetch" },
    credentials: "same-origin",
  }).catch(() => {});
  // Guard against a sync that stalls (e.g. a slow/unreachable Google API): the
  // strip does a single pass (CSS forwards) and reload happens within the cap
  // no matter what, so the sweep never becomes a permanent glow.
  const cap = new Promise((resolve) => window.setTimeout(resolve, 15000));

  // Reload the CURRENT filtered view (not bare "/") so the operator's active
  // period/ready/source/date filters survive the sync — the server also
  // preserves them on the no-JS fallback via a Referer redirect. reload()
  // re-GETs this URL, which pops and shows the session sync_flash too.
  Promise.all([minSweep, Promise.race([request, cap])]).then(() =>
    window.location.reload()
  );
});

// «Видалити Sum3D» (✕ у рядку черги). Clears the row's Sum3D input and fires a
// change event so the form's existing hx-post saves the empty value — the work
// drops back to «можна брати» (its technician path/job_code stays). No-op if the
// input is missing.
document.addEventListener("click", (event) => {
  const btn = event.target.closest(".sum3d-clear");
  if (!btn) return;
  const form = btn.closest(".sum3d-form");
  const input = form && form.querySelector(".sum3d-input");
  if (!input) return;
  input.value = "";
  input.dispatchEvent(new Event("change", { bubbles: true }));
});

// ── «Останні дії» popup (queue header, between undo and redo) ───────────────
// A LOCATOR, not a time machine: clicking an entry never changes data, it only
// scrolls to the work that action touched and highlights its row. Reverting
// stays exclusively on the ← → buttons, so a stray click at the bench is safe.
//
// Same state-machine style as the other menus here: a data-attribute toggled on
// the wrapper, everything delegated on document so it survives HTMX swaps.
function closeActionHistory() {
  document.querySelectorAll("[data-acthist]").forEach((box) => {
    box.classList.remove("is-open");
    const toggle = box.querySelector("[data-acthist-toggle]");
    if (toggle) toggle.setAttribute("aria-expanded", "false");
  });
}

// Highlight the row an action touched. Scrolls INSIDE .tablewrap (the table has
// its own scroll container — scrolling the window instead just yanks the page
// and leaves the row off-screen). The class is re-applied after the 15s poll
// swap below, so a highlight can't be wiped a second after it appears.
//
// focusedUntil bounds that re-application. Without it the poll would restart the
// 2.4s animation every 15 seconds for the rest of the session — a row pulsing
// all day next to an operator at the bench, which is exactly the kind of endless
// loop this screen is not allowed to grow (see CLAUDE.md on the mail-triage
// animation cleanup).
const FOCUS_HIGHLIGHT_MS = 10000;

let focusedOrderId = "";

let focusedUntil = 0;

function highlightOrderRow(orderId, { scroll = true } = {}) {
  const row = document.getElementById("order-row-" + orderId);
  if (!row) return false;
  if (scroll) {
    const wrap = row.closest(".tablewrap");
    if (wrap) {
      const top = row.offsetTop - wrap.clientHeight / 2 + row.offsetHeight / 2;
      wrap.scrollTo({ top: Math.max(0, top), behavior: "smooth" });
    } else {
      row.scrollIntoView({ block: "center", behavior: "smooth" });
    }
  }
  row.classList.remove("row-locate");
  void row.offsetWidth; // restart the animation when the same row is picked twice
  row.classList.add("row-locate");
  return true;
}

document.addEventListener("click", (event) => {
  const toggle = event.target.closest("[data-acthist-toggle]");
  if (toggle) {
    const box = toggle.closest("[data-acthist]");
    const willOpen = !box.classList.contains("is-open");
    closeActionHistory();
    if (willOpen) {
      box.classList.add("is-open");
      toggle.setAttribute("aria-expanded", "true");
    }
    return; // hx-get on the button refetches the list every open
  }

  const entry = event.target.closest(".acthist-row");
  if (entry) {
    const orderId = entry.dataset.actOrder;
    closeActionHistory();
    if (!orderId) return;
    // Archived work has left the queue entirely — its passport is the only
    // place left to look at it.
    if (entry.dataset.actArchived) {
      window.location.href = "/orders/" + orderId;
      return;
    }
    if (highlightOrderRow(orderId)) {
      focusedOrderId = orderId;
      focusedUntil = Date.now() + FOCUS_HIGHLIGHT_MS;
      return;
    }
    // Not on screen: another day tab, or filtered out. Navigate to that day
    // with the filters cleared, and let ?focus= finish the jump after load.
    const tab = entry.dataset.actTab || "";
    const qs = new URLSearchParams({ ready: "all", source: "all", focus: orderId });
    if (tab) qs.set("date", tab);
    window.location.href = "/?" + qs.toString();
    return;
  }

  if (!event.target.closest(".acthist-menu")) closeActionHistory();
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") closeActionHistory();
});

// Arriving from a cross-day jump: ?focus=<id> put the right tab on screen, now
// locate the row. Deferred a tick so the table has laid out and offsetTop is real.
document.addEventListener("DOMContentLoaded", () => {
  const main = document.querySelector(".q2[data-focus-order]");
  if (!main) return;
  focusedOrderId = main.dataset.focusOrder;
  focusedUntil = Date.now() + FOCUS_HIGHLIGHT_MS;
  window.setTimeout(() => highlightOrderRow(focusedOrderId), 80);
});

// The 15s poll replaces #queue-rows wholesale, which would drop the highlight
// mid-look. Re-apply it (without re-scrolling — the operator may have scrolled
// on by then and yanking them back would be worse than losing the tint), but
// only inside the focus window: past it the highlight has done its job and the
// row must go quiet for good.
document.body.addEventListener("htmx:afterSwap", (event) => {
  if (!focusedOrderId) return;
  if (!event.target || event.target.id !== "queue-rows") return;
  if (Date.now() > focusedUntil) {
    focusedOrderId = "";
    return;
  }
  highlightOrderRow(focusedOrderId, { scroll: false });
});

// v2a side-panel accordion (queue.html .side-sec). Toggles data-open on the
// section and aria-expanded on its header. No-op on pages without side-secs.
document.addEventListener("click", (event) => {
  const head = event.target.closest("[data-side-toggle]");
  if (!head) return;
  const sec = head.closest(".side-sec");
  if (!sec) return;
  const open = sec.getAttribute("data-open") === "true";
  sec.setAttribute("data-open", String(!open));
  head.setAttribute("aria-expanded", String(!open));
});

// Date pager placement: when the queue filter bar fits one row, the date pager
// stays inline; when it wraps (narrower screens), drop it onto its own clean
// line aligned under the Період pills. Measured in JS — not a fixed CSS
// breakpoint — so "one row on the big monitor" stays exact whatever its width.
// The measurement is taken with .filters-wrapped removed (natural layout) to
// avoid the class feeding back into its own trigger.
(function () {
  const filters = document.querySelector(".q2 .filters");
  if (!filters) return;

  function update() {
    filters.classList.remove("filters-wrapped");
    // Reflow, then compare the date pager's row against the first filter group.
    void filters.offsetHeight;
    const seg = filters.querySelector(".seg");
    const strip = filters.querySelector(".date-strip");
    if (!seg || !strip) return;
    const wrapped =
      strip.getBoundingClientRect().top - seg.getBoundingClientRect().top > 5;
    filters.classList.toggle("filters-wrapped", wrapped);
  }

  let raf = 0;
  function schedule() {
    cancelAnimationFrame(raf);
    raf = requestAnimationFrame(update);
  }
  update();
  window.addEventListener("resize", schedule);
})();

// Spotlight cards (queue right-rail .side-sec). Writes the cursor position into
// --mx/--my (for the radial glow) and a small tilt into --rx/--ry, per card.
// Adds .spotlight so no-JS cards stay flat. Skips tilt under reduced-motion.
(function () {
  const cards = document.querySelectorAll(".q2 .side-sec");
  if (!cards.length) return;
  const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const MAX_TILT = 4; // degrees — restrained, this is a work tool

  cards.forEach((card) => {
    card.classList.add("spotlight");
    card.addEventListener("mousemove", (e) => {
      const r = card.getBoundingClientRect();
      const x = e.clientX - r.left;
      const y = e.clientY - r.top;
      card.style.setProperty("--mx", x + "px");
      card.style.setProperty("--my", y + "px");
      if (!reduce) {
        const px = x / r.width - 0.5; // -0.5 … 0.5
        const py = y / r.height - 0.5;
        card.style.setProperty("--ry", (px * MAX_TILT).toFixed(2) + "deg");
        card.style.setProperty("--rx", (-py * MAX_TILT).toFixed(2) + "deg");
      }
    });
    card.addEventListener("mouseleave", () => {
      card.style.setProperty("--rx", "0deg");
      card.style.setProperty("--ry", "0deg");
    });
  });
})();

// ---------------------------------------------------------------------------
// Режим вигляду (layout-edit) — ширини стовпців. Щільність і вигляд колонки
// «Матеріал / Колір» ПЕРЕЇХАЛИ звідси в шестерню над таблицею (lookgear.js) і
// живуть на акаунті оператора: вони мають їхати за людиною, а не за браузером.
// Ширини лишились тут свідомо — вони прив'язані до конкретного монітора.
(function () {
  const LS_MODE = "layoutEditMode";
  const LS_WIDTHS = "queueColWidths";
  const MIN_COL = 60;

  function lsGet(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
  function lsSet(k, v) { try { localStorage.setItem(k, v); } catch (e) {} }
  function lsDel(k) { try { localStorage.removeItem(k); } catch (e) {} }

  // Live lookup — the queue table is re-rendered by the 15s #queue-rows poll,
  // so a reference captured once would go stale. Every helper re-queries.
  // ОБОВ'ЯЗКОВО скоупити на .tablewrap: легенда кольорів («Кольори» в шапці)
  // має власну <table class="qtable"> у .qlegend-rows, і вона стоїть у DOM
  // ВИЩЕ черги — без скоупу querySelector хапав саме її, і весь код ширин
  // колонок мовчки працював на легенді, а не на черзі.
  function getTable() { return document.querySelector(".q2 .tablewrap table.qtable"); }

  // ---- Ширини стовпців -----------------------------------------------------
  function headCells() {
    const table = getTable();
    if (!table || !table.tHead || !table.tHead.rows[0]) return [];
    return Array.from(table.tHead.rows[0].cells);
  }
  function loadWidths() {
    try { return JSON.parse(lsGet(LS_WIDTHS)) || null; } catch (e) { return null; }
  }
  function applySavedWidths() {
    const table = getTable();
    const map = loadWidths();
    if (!map || !table) return;
    const cells = headCells();
    if (!cells.length) return;
    // Стара мапа могла зберегтись для іншої таблиці (див. getTable — легенда
    // раніше перехоплювала getTable, а її .qtable має лише одну колонку).
    // У такій мапі стовпців помітно менше, ніж у черзі — це не наші ширини:
    // чистимо й виходимо, щоб не спотворити чергу застарілими значеннями.
    if (Object.keys(map).length < cells.length - 1) { lsDel(LS_WIDTHS); return; }
    // Ширини збережені під конкретний монітор. Той самий браузер на вужчому
    // екрані (спільний localStorage 24"/27") діставав суму, більшу за доступне,
    // і виштовхував останню колонку (Оператор) за край. Масштабуємо пропорційно
    // до реальної ширини скрол-контейнера, щоб набір завжди влазив.
    const wrap = table.closest(".tablewrap");
    const avail = wrap ? wrap.clientWidth : 0;
    let sum = 0;
    cells.forEach((th, i) => { if (map[i]) sum += map[i]; });
    const factor = (avail && sum > avail) ? avail / sum : 1;
    table.style.tableLayout = "fixed";
    cells.forEach((th, i) => {
      if (map[i]) th.style.width = Math.max(MIN_COL, Math.round(map[i] * factor)) + "px";
    });
  }
  // Перед першим перетягуванням фіксуємо поточні (auto) ширини всіх стовпців,
  // щоб table-layout:fixed не перерозподілив їх стрибком.
  function freezeWidths() {
    const table = getTable();
    if (!table) return {};
    const cells = headCells();
    const rects = cells.map((th) => Math.round(th.getBoundingClientRect().width));
    table.style.tableLayout = "fixed";
    const map = loadWidths() || {};
    cells.forEach((th, i) => {
      if (!th.style.width) th.style.width = rects[i] + "px";
      map[i] = parseInt(th.style.width, 10) || rects[i];
    });
    return map;
  }
  function saveCurrentWidths() {
    const map = {};
    headCells().forEach((th, i) => {
      const w = parseInt(th.style.width, 10);
      if (w) map[i] = w;
    });
    lsSet(LS_WIDTHS, JSON.stringify(map));
  }

  // Pointer-drag на межі стовпця (.colgrip у _queue_table_head.html).
  let drag = null;
  function onPointerDown(event) {
    const grip = event.target.closest("[data-col-resize]");
    if (!grip || !document.body.classList.contains("layout-edit")) return;
    const th = grip.closest("th");
    if (!th) return;
    event.preventDefault();
    event.stopPropagation();
    freezeWidths();
    drag = {
      grip: grip,
      th: th,
      startX: event.clientX,
      startW: parseInt(th.style.width, 10) || Math.round(th.getBoundingClientRect().width),
    };
    grip.classList.add("is-dragging");
    try { if (grip.setPointerCapture && event.pointerId != null) grip.setPointerCapture(event.pointerId); } catch (e) {}
    window.addEventListener("pointermove", onPointerMove);
    window.addEventListener("pointerup", onPointerUp);
  }
  function onPointerMove(event) {
    if (!drag) return;
    const w = Math.max(MIN_COL, drag.startW + (event.clientX - drag.startX));
    drag.th.style.width = w + "px";
  }
  function onPointerUp() {
    if (!drag) return;
    drag.grip.classList.remove("is-dragging");
    drag = null;
    window.removeEventListener("pointermove", onPointerMove);
    window.removeEventListener("pointerup", onPointerUp);
    saveCurrentWidths();
  }
  // Не давати кліку по межі тригерити сортування заголовка.
  document.addEventListener("click", (event) => {
    if (event.target.closest("[data-col-resize]")) {
      event.preventDefault();
      event.stopPropagation();
    }
  }, true);

  // ---- Активація режиму + скидання -----------------------------------------
  function setMode(on) {
    document.body.classList.toggle("layout-edit", on);
    lsSet(LS_MODE, on ? "1" : "0");
    document.querySelectorAll("[data-layout-edit-toggle]").forEach((btn) => {
      btn.setAttribute("aria-pressed", String(on));
    });
  }
  // Ширини стовпців чистить лише цей код — решту вигляду скидає шестерня,
  // і саме вона надсилає подію, тому «Скинути» лишається одним рухом.
  function resetLayout() {
    lsDel(LS_WIDTHS);
    const table = getTable();
    if (table) {
      table.style.tableLayout = "";
      headCells().forEach((th) => { th.style.width = ""; });
    }
  }

  // Wire-up (no-op на сторінках без черги/кнопки).
  applySavedWidths();
  document.querySelectorAll("[data-layout-edit-toggle]").forEach((btn) => {
    btn.setAttribute("aria-pressed", String(document.body.classList.contains("layout-edit")));
    btn.addEventListener("click", () => setMode(!document.body.classList.contains("layout-edit")));
  });
  // Кнопки [data-layout-reset] у розмітці більше немає — скидання живе в
  // шестерні, яка шле цю подію. Обробник на неіснуючий селектор лишав би
  // враження, що кнопка ще десь є.
  document.addEventListener("queue-look-reset", resetLayout);
  // Delegated on document (not the table element) so column-resize keeps
  // working after the 15s poll swaps the table.
  document.addEventListener("pointerdown", onPointerDown);
  // Re-apply the operator's saved column widths whenever the poll replaces the
  // rows block (a fresh <thead> comes back with no inline widths otherwise).
  document.addEventListener("htmx:afterSwap", (event) => {
    if (event.target && event.target.id === "queue-rows") applySavedWidths();
  });
})();

// Мікроанімації черги (задача 3). Новий рядок після HTMX-свапу статусу/Sum3D
// коротко флешить (підтвердження зміни); прострочені/нові рядки при першому
// рендері елегантно з'являються. `prefers-reduced-motion` вимикає анімації
// через CSS (@media reduce), тож тут додатковий guard не потрібен — клас лише
// вмикає CSS-анімацію, яку reduce-медіа занулює.
document.addEventListener("htmx:afterSwap", (event) => {
  const row = event.target && event.target.closest ? event.target.closest("tr.queue-row") : null;
  const el = row || (event.detail && event.detail.target);
  if (el && el.classList && el.classList.contains("queue-row")) {
    el.classList.remove("row-flash");
    void el.offsetWidth;
    el.classList.add("row-flash");
    window.setTimeout(() => el.classList.remove("row-flash"), 1000);
  }
});

// v2a job-passport slide-over (queue.html .detail-pane). Clicking a наряд/вид
// link ([data-order-detail]) opens /orders/{id} inside the pane's iframe
// instead of navigating — the real order_detail page, all logic intact.
// Without JS the same link just navigates (graceful fallback). Esc / backdrop
// / close button dismiss it.
(function () {
  const pane = document.getElementById("order-detail-pane");
  if (!pane) return;
  const backdrop = document.getElementById("order-detail-backdrop");
  const frame = document.getElementById("order-detail-frame");
  const title = document.getElementById("order-detail-title");
  const closeBtn = document.getElementById("order-detail-close");

  function open(id) {
    frame.src = "/orders/" + encodeURIComponent(id);
    if (title) title.textContent = "Наряд · " + id;
    pane.classList.add("open");
    pane.setAttribute("aria-hidden", "false");
    if (backdrop) backdrop.classList.add("open");
  }
  function close() {
    pane.classList.remove("open");
    pane.setAttribute("aria-hidden", "true");
    if (backdrop) backdrop.classList.remove("open");
    // clear the iframe so its state resets next open
    window.setTimeout(() => { if (!pane.classList.contains("open")) frame.src = "about:blank"; }, 250);
  }

  document.addEventListener("click", (event) => {
    const link = event.target.closest("[data-order-detail]");
    if (!link) return;
    // let modified clicks (new tab, etc.) behave normally
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.button !== 0) return;
    event.preventDefault();
    open(link.getAttribute("data-order-detail"));
  });
  if (closeBtn) closeBtn.addEventListener("click", close);
  if (backdrop) backdrop.addEventListener("click", close);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && pane.classList.contains("open")) close();
  });
})();

// ── Пришпилена робота їде нагору ────────────────────────────────────────
// Сервер сортує пришпилені вгору і після кліку шле refresh-queue, але повне
// перемальовування черги на шести сотнях рядків коштує ~3.5 с. Стільки
// «нічого не сталось» після кліку читається як зламана кнопка, тому рядок
// переїжджає на клієнті, а серверний свап потім лише підтверджує видиме.
//
// Їде, а не телепортується: FLIP — запам'ятати, де рядок був, перенести в
// DOM, і зіграти різницю трансформом. Стрибок через півсотні рядків без руху
// не читається як «мій рядок піднявся» — читається як «список перемішався»,
// а це рівно та тривога, від якої в цьому проєкті є писане правило.
// Анімується transform, не позиція: рядок таблиці, зсунутий геометрично,
// перекладає всю таблицю на кожному кадрі.
//
// Дві пастки, на які це наткнулось по черзі (обидві мовчазні):
//  1. Рядок не можна шукати через event.target: свап іде outerHTML, тому
//     подія приходить на БАТЬКА заміненого <tr>, і closest("tr") там нічого
//     не знаходить. Шукаємо за шляхом запиту.
//  2. Слухати треба afterSettle, а не afterSwap: на afterSwap у DOM ще висить
//     СТАРИЙ рядок (htmx-swapping), тобто getElementById віддає його — без
//     свіжого класу queue-row-focus, і перевірка тихо відсіювала рух.
//
// Рухаємо ВСЕРЕДИНІ свого tbody: черга поділена на «лабораторні» й «з пошти»,
// і рядок, що перестрибнув у чужу секцію, збрехав би про джерело роботи.
// Знімаючи шпильку, рядок на місце НЕ повертаємо: куди саме — знає лише
// сервер, а стрибок під курсором одразу після кліку гірший за рядок, який
// спокійно стане на місце з наступним оновленням.
document.addEventListener("htmx:afterSettle", (event) => {
  const cfg = event.detail && event.detail.requestConfig;
  const path = (cfg && cfg.path) || "";
  const match = path.match(/^\/orders\/(\d+)\/focus$/);
  if (!match) return;
  const row = document.getElementById("order-row-" + match[1]);
  if (!row || !row.classList.contains("queue-row-focus")) return;
  const body = row.parentElement;
  if (!body) return;

  // В КІНЕЦЬ набору, а не вгору. Сервер шикує пришпилені за часом
  // пришпилення (services/focus.ranks), і клієнт мусить тримати рівно той
  // самий порядок — інакше кожна нова шпилька спершу стрибала вгору, а через
  // кілька секунд серверне оновлення повертало її на місце, і весь набір
  // перемішувався під рукою. Заразом це найспокійніший порядок: жоден уже
  // пришпилений рядок не рухається взагалі, набір росте вниз.
  let anchor = body.firstElementChild;
  while (anchor && anchor !== row && anchor.classList.contains("queue-row-focus")) {
    anchor = anchor.nextElementSibling;
  }
  if (anchor === row) return; // уже стоїть одразу за набором

  const from = row.getBoundingClientRect().top;
  body.insertBefore(row, anchor);
  const to = row.getBoundingClientRect().top;
  const shift = from - to;
  if (!shift) return;

  // Прокрутити до верху списку, інакше рядок «поїде» туди, де оператор його
  // не бачить, і рух втратить весь сенс.
  const scroller = row.closest(".tablewrap");
  if (scroller && scroller.scrollTop > 0) scroller.scrollTo({ top: 0, behavior: "smooth" });

  if (window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  row.classList.add("row-rising");
  row.style.transform = "translateY(" + shift + "px)";
  // Один кадр із початковим зсувом — інакше браузер згорне обидві зміни в
  // одну й анімації не буде взагалі.
  requestAnimationFrame(() => {
    requestAnimationFrame(() => {
      row.style.transition = "transform .42s cubic-bezier(.22,1,.36,1)";
      row.style.transform = "translateY(0)";
    });
  });
  // Слухаємо САМЕ transform САМЕ цього рядка. Без перевірки підйом обривався
  // майже завжди: рядок має власний `transition: background .14s`, а
  // .row-rising знімає з нього :hover — тобто фон одразу починає переходити і
  // через ~140 мс кидає transitionend, який обнуляв transform посеред
  // 420-мілісекундного руху. Плюс сюди спливали переходи дочірніх кнопок.
  let guard = null;
  const done = (event) => {
    if (event && (event.target !== row || event.propertyName !== "transform")) return;
    window.clearTimeout(guard);
    row.style.transition = "";
    row.style.transform = "";
    row.classList.remove("row-rising");
    row.removeEventListener("transitionend", done);
  };
  row.addEventListener("transitionend", done);
  // Страховка: transitionend не прийде, якщо рядок замінить полл на півдорозі.
  guard = window.setTimeout(done, 700);
});


// ── Секція печей у бічній панелі: відкритість живе на body ───────────────
// Секція свапається цілком кожні 30 с (власний полл), тому стан відкриття не
// можна тримати атрибутом на ній: він помирав разом зі старою розміткою, і
// розгорнуті плитки самі схлопувались менш ніж за півхвилини. Клас на body
// свап переживає — той самий прийом, що й у смуги печей над чергою.
const FURNACE_SIDE_KEY = "furnaceSideOpen";

document.addEventListener("click", (event) => {
  if (!event.target.closest("[data-furnace-side-toggle]")) return;
  const open = document.body.classList.toggle("furnace-side-open");
  try { localStorage.setItem(FURNACE_SIDE_KEY, open ? "1" : "0"); } catch (e) { /* приватний режим */ }
  document.querySelectorAll("[data-furnace-side-toggle]").forEach((btn) => {
    btn.setAttribute("aria-expanded", String(open));
  });
});

// aria мусить збігатися з класом і ПІСЛЯ свапу: нова розмітка приходить без
// нього, і для скрінрідера секція лишалась би «згорнутою» завжди.
document.addEventListener("htmx:afterSettle", (event) => {
  const swapped = event.target && event.target.id === "furnace-side";
  if (!swapped) return;
  const open = document.body.classList.contains("furnace-side-open");
  document.querySelectorAll("[data-furnace-side-toggle]").forEach((btn) => {
    btn.setAttribute("aria-expanded", String(open));
  });
});

// Верстати — та сама механіка, що в пічок (клас на body переживає 30-секундний
// свап секції; атрибут на ній помирав би разом зі старою розміткою).
const MACHINE_SIDE_KEY = "machineSideOpen";

document.addEventListener("click", (event) => {
  if (!event.target.closest("[data-machine-side-toggle]")) return;
  const open = document.body.classList.toggle("machine-side-open");
  try { localStorage.setItem(MACHINE_SIDE_KEY, open ? "1" : "0"); } catch (e) { /* приватний режим */ }
  document.querySelectorAll("[data-machine-side-toggle]").forEach((btn) => {
    btn.setAttribute("aria-expanded", String(open));
  });
});

document.addEventListener("htmx:afterSettle", (event) => {
  const swapped = event.target && event.target.id === "machine-side";
  if (!swapped) return;
  const open = document.body.classList.contains("machine-side-open");
  document.querySelectorAll("[data-machine-side-toggle]").forEach((btn) => {
    btn.setAttribute("aria-expanded", String(open));
  });
});

// ── Згортання правої side-панелі ──────────────────────────────────────────
// Звільняє ~296px для таблиці, коли колонки не влазять (вибір власника: без
// втрати даних). Стан — у localStorage. Якщо користувач ще НЕ вибирав, панель
// авто-згортається, коли таблиця не влазить у свій контейнер — тоді дефолт
// «влазить» сам. Клас живе на <body>, тому переживає 15с-свап #queue-rows.
(function () {
  const KEY = "qsideCollapsed";
  function lsGet(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
  function lsSet(k, v) { try { localStorage.setItem(k, v); } catch (e) {} }

  function apply(on) {
    document.body.classList.toggle("qside-collapsed", on);
    document.querySelectorAll("[data-qside-toggle]").forEach((btn) => {
      btn.classList.toggle("is-on", on);
      btn.setAttribute("aria-pressed", String(on));
    });
  }

  function tableOverflows() {
    const wrap = document.querySelector(".tablewrap");
    return !!wrap && wrap.scrollWidth - wrap.clientWidth > 4;
  }

  // Початковий стан: явний вибір користувача переважає; інакше — авто за overflow.
  // Anti-flash у base.html уже міг поставити клас для збереженого "1", але тут
  // ми ще й синхронізуємо кнопку та обробляємо авто-випадок.
  const saved = lsGet(KEY);
  if (saved === "1") apply(true);
  else if (saved === "0") apply(false);
  else apply(tableOverflows());

  document.addEventListener("click", function (event) {
    if (!event.target.closest("[data-qside-toggle]")) return;
    const on = !document.body.classList.contains("qside-collapsed");
    apply(on);
    lsSet(KEY, on ? "1" : "0");
  });
})();

// ── Привид Sum3D: підказка захопленого проєкту в пришпиленому рядку ────────
// Сервер малює її лише там, де рядок у «мої зараз», поле порожнє і з теки
// Cam-work щойно захоплено проєкт (див. _order_row.html). Тут — прийняття:
// клік по привиду або Enter у порожньому полі підставляє значення й шле форму
// тим самим шляхом, що й ручний ввід (лог + «Крок назад» працюють як завжди).
function acceptSum3dGhost(input, value) {
  if (!input || !value) return;
  input.value = value;
  input.classList.remove("has-ghost");
  const ghost = input.parentElement && input.parentElement.querySelector(".sum3d-ghost");
  if (ghost) ghost.remove();
  // change — той самий тригер, який слухає hx-post форми рядка.
  input.dispatchEvent(new Event("change", { bubbles: true }));
}

document.addEventListener("click", (event) => {
  const ghost = event.target.closest("[data-ghost-accept]");
  if (!ghost) return;
  event.preventDefault();
  const form = ghost.closest(".sum3d-form");
  const input = form && form.querySelector(".sum3d-input");
  acceptSum3dGhost(input, ghost.dataset.ghostAccept);
});

// Enter у ПОРОЖНЬОМУ полі з привидом = прийняти підказку. Порожнє поле інакше
// не має що зберігати, тож ми нічого не перехоплюємо в оператора: якщо він
// почав друкувати своє — привида вже немає (клас знято на першому ж вводі).
document.addEventListener("keydown", (event) => {
  if (event.key !== "Enter") return;
  const input = event.target.closest(".sum3d-input.has-ghost");
  if (!input || input.value.trim()) return;
  event.preventDefault();
  acceptSum3dGhost(input, input.dataset.ghost);
});

// Почав друкувати своє — підказка більше не потрібна й не має заважати.
document.addEventListener("input", (event) => {
  const input = event.target.closest(".sum3d-input.has-ghost");
  if (!input) return;
  input.classList.remove("has-ghost");
  const ghost = input.parentElement && input.parentElement.querySelector(".sum3d-ghost");
  if (ghost) ghost.remove();
});

// Панель «ще N захоплених Sum3D». Стан — клас на body: сам лоток свапається
// поллом кожні 15с, і атрибут на ньому помирав би разом зі старою розміткою
// (той самий урок, що зі смугою пічок).
document.addEventListener("click", (event) => {
  if (event.target.closest("[data-sum3d-more]")) {
    const open = document.body.classList.toggle("sum3d-panel-open");
    document.querySelectorAll("[data-sum3d-more]").forEach((b) =>
      b.setAttribute("aria-expanded", String(open))
    );
    return;
  }
  // Клік повз панель — закрити (копія всередині панелі не закриває).
  if (
    document.body.classList.contains("sum3d-panel-open") &&
    !event.target.closest(".sum3d-tray")
  ) {
    document.body.classList.remove("sum3d-panel-open");
  }
});
