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

// Inline "add work" form in the queue card-head: the "+" toggle reveals the
// client-work fields right above the rows; cancel hides them. Delegated so it
// survives HTMX swaps.
document.addEventListener("click", (event) => {
  const toggle = event.target.closest("[data-addwork-toggle]");
  if (toggle) {
    const form = document.querySelector("[data-addwork]");
    if (!form) return;
    const open = form.hidden;
    form.hidden = !open;
    toggle.setAttribute("aria-expanded", String(open));
    if (open) {
      const first = form.querySelector("input");
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
  }
});

document.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-copy]");
  if (!button) return;

  const value = button.dataset.copy || "";
  if (!value) return;

  const originalTitle = button.title;
  try {
    await navigator.clipboard.writeText(value);
  } catch (_error) {
    const input = document.createElement("textarea");
    input.value = value;
    input.setAttribute("readonly", "");
    input.style.position = "fixed";
    input.style.opacity = "0";
    document.body.appendChild(input);
    input.select();
    document.execCommand("copy");
    input.remove();
  }

  button.title = "Скопійовано";
  button.classList.add("copy-success");
  window.setTimeout(() => {
    button.title = originalTitle;
    button.classList.remove("copy-success");
  }, 1400);
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

// Collapsible "Нові з пошти" block on the queue dashboard (CLAUDE.md section
// 8): starts collapsed to a single summary row, expands on click. No-op on
// every other page — the toggle button only exists on the queue screen.
document.addEventListener("click", (event) => {
  const toggle = event.target.closest("[data-pending-mail-toggle]");
  if (!toggle) return;

  const section = toggle.closest(".pending-mail");
  if (!section) return;

  const collapsed = section.classList.toggle("is-collapsed");
  toggle.setAttribute("aria-expanded", String(!collapsed));
});

// Same collapse mechanism as "Нові з пошти" above (is-collapsed class +
// aria-expanded + CSS grid-template-rows transition, see .queue-group-*
// rules in base.css), reused for the queue table's "Лабораторні роботи" /
// "Роботи з пошти" section headers (queue.html) — both start expanded, so
// unlike the pending-mail toggle this one only ever removes is-collapsed
// on first click, never starts with it already applied server-side.
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

// Themed update overlay (app/templates/_update_overlay.html, styles
// .update-* in base.css). When an admin clicks "Встановити" on the rail
// update banner (form[action="/settings/update/install"]), instead of the
// plain POST→/settings flash we show a full-screen milling animation, fire
// the install request in the background, cycle mono status lines, and reload
// the page once the app has restarted and /health answers again. Without JS
// the form submits normally (graceful fallback to the flash text). The show
// function is also exposed for manual verification (window.showUpdateOverlay).
(function () {
  const STAGES = [
    "Завантаження оновлення…",
    "Перевірка контрольної суми…",
    "Розпакування пакета…",
    "Встановлення файлів…",
    "Перезапуск… за мить сторінка оновиться",
  ];
  const STAGE_MS = 2200;

  let stageTimer = null;
  let healthTimer = null;
  let shown = false;

  function showUpdateOverlay() {
    const overlay = document.getElementById("update-overlay");
    if (!overlay || shown) return;
    shown = true;

    const statusEl = document.getElementById("update-status");
    overlay.hidden = false;
    overlay.setAttribute("aria-hidden", "false");
    // Force reflow so the fade-in transition runs from the hidden state
    // (rAF is throttled if the tab isn't painting, so don't rely on it).
    void overlay.offsetWidth;
    overlay.classList.add("is-shown");

    // Cycle the mono status lines; hold on the final "Перезапуск…" stage.
    let i = 0;
    if (statusEl) {
      statusEl.textContent = STAGES[0];
      stageTimer = window.setInterval(() => {
        if (i >= STAGES.length - 1) {
          window.clearInterval(stageTimer);
          stageTimer = null;
          return;
        }
        i += 1;
        statusEl.classList.add("is-fading");
        window.setTimeout(() => {
          statusEl.textContent = STAGES[i];
          statusEl.classList.remove("is-fading");
        }, 180);
      }, STAGE_MS);
    }

    startHealthReloadPoll();
  }

  // Poll /health. The app is about to restart, so /health will first start
  // failing (connection dropped) and then, once the new process is up, answer
  // 200 again — that transition (a failure THEN a success) is our signal to
  // reload into the freshly updated app. Reloading only after an observed
  // failure avoids reloading the still-old process before it has restarted.
  function startHealthReloadPoll() {
    let sawFailure = false;
    healthTimer = window.setInterval(() => {
      fetch("/health", { cache: "no-store" })
        .then((r) => {
          if (!r.ok) throw new Error("bad");
          if (sawFailure) {
            window.clearInterval(healthTimer);
            healthTimer = null;
            window.location.reload();
          }
        })
        .catch(() => {
          sawFailure = true;
        });
    }, 1500);
  }

  window.showUpdateOverlay = showUpdateOverlay;

  document.addEventListener("submit", (event) => {
    const form = event.target.closest('form[action="/settings/update/install"]');
    if (!form) return;
    event.preventDefault();

    const button = form.querySelector('button[type="submit"]');
    if (button) button.disabled = true;

    showUpdateOverlay();

    // Fire the real install request; the response never really arrives (the
    // app restarts mid-flight), so a rejected/aborted fetch is expected and
    // ignored — the health poll drives the reload.
    fetch("/settings/update/install", {
      method: "POST",
      headers: { "X-Requested-With": "fetch" },
      credentials: "same-origin",
    }).catch(() => {});
  });
})();

// Left-rail collapse toggle. Persists in localStorage; the anti-flash inline
// script in base.html applies the saved state before first paint, so this only
// handles the click and keeps the stored value in sync. No-op if the rail
// button isn't on the page (e.g. login/license screens have no rail).
document.addEventListener("click", (event) => {
  const btn = event.target.closest("[data-rail-collapse]");
  if (!btn) return;
  const collapsed = document.body.classList.toggle("rail-collapsed");
  try {
    localStorage.setItem("railCollapsed", collapsed ? "1" : "0");
  } catch (_error) {
    /* private mode / storage disabled — toggle still works for this page */
  }
  btn.setAttribute("aria-label", collapsed ? "Розгорнути меню" : "Згорнути меню");
  btn.setAttribute("title", collapsed ? "Розгорнути меню" : "Згорнути меню");
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

// Drag-and-drop file zone (settings backup restore, .dropzone wrapping a hidden
// file input). Click already works via the native <label>; this adds drag/drop
// and shows the chosen filename. No-op on pages without a .dropzone.
(function () {
  const zone = document.getElementById("backup-dropzone");
  if (!zone) return;
  const input = zone.querySelector(".dropzone-input");
  const fileEl = zone.querySelector(".dropzone-file");
  const titleEl = zone.querySelector(".dropzone-title");
  const hintEl = zone.querySelector(".dropzone-hint");
  if (!input) return;

  function showFile(name) {
    if (!name) {
      zone.classList.remove("has-file");
      if (fileEl) { fileEl.hidden = true; fileEl.textContent = ""; }
      if (titleEl) titleEl.hidden = false;
      if (hintEl) hintEl.hidden = false;
      return;
    }
    zone.classList.add("has-file");
    if (titleEl) titleEl.hidden = true;
    if (hintEl) hintEl.hidden = true;
    if (fileEl) { fileEl.hidden = false; fileEl.textContent = name; }
  }

  input.addEventListener("change", () => {
    showFile(input.files && input.files[0] ? input.files[0].name : "");
  });

  ["dragenter", "dragover"].forEach((evt) =>
    zone.addEventListener(evt, (e) => {
      e.preventDefault();
      zone.classList.add("is-dragover");
    })
  );
  ["dragleave", "dragend"].forEach((evt) =>
    zone.addEventListener(evt, (e) => {
      // Ignore dragleave bubbling from children still inside the zone.
      if (evt === "dragleave" && zone.contains(e.relatedTarget)) return;
      zone.classList.remove("is-dragover");
    })
  );
  zone.addEventListener("drop", (e) => {
    e.preventDefault();
    zone.classList.remove("is-dragover");
    const files = e.dataTransfer && e.dataTransfer.files;
    if (!files || !files.length) return;
    input.files = files; // assigning a FileList is supported for drops
    input.dispatchEvent(new Event("change", { bubbles: true }));
  });
})();

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
// Режим вигляду (layout-edit) — задача 1. Оператор вмикає режим і налаштовує
// чергу під себе: ширини стовпців (drag), щільність рядків і карток. Усе
// локально в localStorage, без запитів на сервер і без змін маршрутів. Anti-flash
// (застосування збереженого стану до першого рендеру) — інлайн у base.html.
(function () {
  const LS_MODE = "layoutEditMode";
  const LS_DENSITY = "queueDensity";
  const LS_WIDTHS = "queueColWidths";
  const MIN_COL = 60;
  const DENSITIES = ["compact", "normal", "spacious"];

  function lsGet(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
  function lsSet(k, v) { try { localStorage.setItem(k, v); } catch (e) {} }
  function lsDel(k) { try { localStorage.removeItem(k); } catch (e) {} }

  // Live lookup — the queue table is re-rendered by the 15s #queue-rows poll,
  // so a reference captured once would go stale. Every helper re-queries.
  function getTable() { return document.querySelector(".q2 table.qtable"); }

  // ---- Щільність -----------------------------------------------------------
  function currentDensity() {
    const d = lsGet(LS_DENSITY);
    return DENSITIES.indexOf(d) >= 0 ? d : "normal";
  }
  function applyDensity(d) {
    if (DENSITIES.indexOf(d) < 0) d = "normal";
    // "normal" — дефолт токенів, атрибут не потрібен (тримає розмітку чистою).
    if (d === "normal") document.body.removeAttribute("data-density");
    else document.body.setAttribute("data-density", d);
    lsSet(LS_DENSITY, d);
    document.querySelectorAll("[data-density-set]").forEach((b) => {
      b.classList.toggle("is-active", b.getAttribute("data-density-set") === d);
    });
  }

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
    table.style.tableLayout = "fixed";
    headCells().forEach((th, i) => {
      if (map[i]) th.style.width = map[i] + "px";
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
  function resetLayout() {
    lsDel(LS_WIDTHS);
    lsDel(LS_DENSITY);
    const table = getTable();
    if (table) {
      table.style.tableLayout = "";
      headCells().forEach((th) => { th.style.width = ""; });
    }
    applyDensity("normal");
  }

  // Wire-up (no-op на сторінках без черги/кнопки).
  applySavedWidths();
  applyDensity(currentDensity());
  document.querySelectorAll("[data-layout-edit-toggle]").forEach((btn) => {
    btn.setAttribute("aria-pressed", String(document.body.classList.contains("layout-edit")));
    btn.addEventListener("click", () => setMode(!document.body.classList.contains("layout-edit")));
  });
  document.querySelectorAll("[data-density-set]").forEach((btn) => {
    btn.addEventListener("click", () => applyDensity(btn.getAttribute("data-density-set")));
  });
  document.querySelectorAll("[data-layout-reset]").forEach((btn) => {
    btn.addEventListener("click", resetLayout);
  });
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
