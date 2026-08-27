// Restore scroll position across a full page reload triggered by a plain
// (non-htmx) form POST — e.g. the "+ Додати" manual-add form, whose
// /orders/new route redirects back to "/" on success. A fresh navigation
// always starts scrolled to top, so the submit handler stashes the current
// position in sessionStorage right before the browser navigates away; this
// consumes it once on the next load so a normal (non-restore) visit is
// unaffected.
(function restoreScrollAfterReload() {
  const saved = sessionStorage.getItem("od-scroll");
  const savedTable = sessionStorage.getItem("od-tablescroll");
  if (saved === null && savedTable === null) return;
  sessionStorage.removeItem("od-scroll");
  sessionStorage.removeItem("od-tablescroll");
  const y = parseInt(saved, 10);
  if (!Number.isNaN(y)) window.scrollTo(0, y);
  // The queue table scrolls INSIDE .tablewrap, not the window (see the poll
  // guard below), so the manual-add reload lands the operator at the top of
  // the list unless we restore the container's own scrollTop. Deferred to the
  // next frame so the table has laid out its full height first.
  const t = parseInt(savedTable, 10);
  if (!Number.isNaN(t) && t > 0) {
    // The table's full height isn't laid out the instant this deferred script
    // runs, so a single set can be clamped to 0. Re-apply across a few frames
    // and once more on window 'load' (fonts/layout settled), stopping as soon
    // as it sticks — cheap, and it survives a slow first paint.
    let tries = 0;
    const apply = function () {
      const wrap = document.querySelector(".tablewrap");
      if (wrap && wrap.scrollHeight > wrap.clientHeight) {
        wrap.scrollTop = t;
        if (Math.abs(wrap.scrollTop - t) < 2) return; // landed
      }
      if (tries++ < 20) requestAnimationFrame(apply);
    };
    requestAnimationFrame(apply);
    window.addEventListener("load", apply, { once: true });
  }
})();

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

// Same two-layer poll guard for the mail triage list (#mail-list-rows, polled
// every 15s). The list scrolls inside .listwrap; an outerHTML swap of its only
// child would reset that to the top. Byte-identical response → skip swap. Most
// rows carry hx-preserve so the swap is cheap and never re-animates them; this
// just keeps the operator's scroll position when the set actually changes.
let lastMailListResponse = null;
let savedMailScroll = null;
document.addEventListener("htmx:beforeSwap", (event) => {
  const target = event.detail.target;
  if (!target || target.id !== "mail-list-rows") return;
  const incoming = event.detail.serverResponse;
  if (incoming != null && incoming === lastMailListResponse) {
    event.detail.shouldSwap = false;
    return;
  }
  lastMailListResponse = incoming;
  const wrap = target.closest(".listwrap");
  savedMailScroll = wrap ? wrap.scrollTop : null;
});
document.addEventListener("htmx:afterSettle", (event) => {
  if (savedMailScroll == null) return;
  const el = event.detail && event.detail.elt;
  if (el && el.id === "mail-list-rows") {
    const wrap = el.closest(".listwrap");
    if (wrap) wrap.scrollTop = savedMailScroll;
    savedMailScroll = null;
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

// Collapsible client card on the handout screen (Ранкова видача): the chevron
// in each card head folds the card body (works list + export folders) away so
// a long day's list stays scannable. Client-side only, starts expanded; the
// "Видати" button and progress in the head stay visible while collapsed.
document.addEventListener("click", (event) => {
  const toggle = event.target.closest(".card-collapse");
  if (!toggle) return;

  const card = toggle.closest(".ccard");
  if (!card) return;

  const collapsed = card.classList.toggle("is-collapsed");
  toggle.setAttribute("aria-expanded", String(!collapsed));
});

// Handout view switch: "Рядки" (compact list, default) ⇄ "Плитки" (preview
// tiles). Same markup, two CSS layouts (.clients / .clients.as-tiles). The
// choice is remembered per-browser in localStorage so it survives navigation
// and reloads. No-op on every other screen.
const HANDOUT_VIEW_KEY = "handout-view";

function applyHandoutView(mode) {
  const root = document.querySelector("[data-view-root]");
  if (!root) return;
  const tiles = mode === "tiles";
  root.classList.toggle("as-tiles", tiles);
  document.querySelectorAll(".view-btn").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.viewMode === (tiles ? "tiles" : "rows"));
  });
}

document.addEventListener("click", (event) => {
  const btn = event.target.closest(".view-btn");
  if (!btn) return;
  const mode = btn.dataset.viewMode === "tiles" ? "tiles" : "rows";
  try {
    window.localStorage.setItem(HANDOUT_VIEW_KEY, mode);
  } catch (_) {
    /* private mode / storage disabled — the toggle still works this session */
  }
  applyHandoutView(mode);
});

document.addEventListener("DOMContentLoaded", () => {
  if (!document.querySelector("[data-view-root]")) return;
  let saved = "rows";
  try {
    saved = window.localStorage.getItem(HANDOUT_VIEW_KEY) || "rows";
  } catch (_) {
    /* ignore */
  }
  applyHandoutView(saved);
});

// Mail triage: highlight the row whose letter is open in the right-hand detail
// panel. The HTMX get itself fills #mail-detail; this only tracks which row is
// the active one so the master-detail selection reads clearly.
document.addEventListener("click", (event) => {
  const row = event.target.closest(".mailrow");
  if (!row) return;
  if (event.target.closest(".mail-reject-form")) return; // reject button isn't a selection
  document.querySelectorAll(".mailrow.active").forEach((r) => r.classList.remove("active"));
  row.classList.add("active");
  // Opening a letter clears its "unread by me" highlight instantly — the GET
  // also stamps seen_at server-side, this just keeps the UI honest before the
  // next poll/reload.
  row.classList.remove("unread");
  const dot = row.querySelector(".newdot");
  if (dot) dot.remove();
});

// Segmented triage detail (variant C — _mail_detail_panel.html): [Лист] /
// [Файли + STL] / [Прийняти] tabs + a sticky action bar. The active segment is
// remembered across full-panel HTMX re-renders (extract-archives, fetch-link,
// accept all swap #mail-detail), and reset to "letter" when a DIFFERENT letter
// is opened. The sticky "Прийняти →" button (data-seg-go) just activates the
// accept tab — the real accept stays the wizard's own step-3 submit.
let mailSeg = "letter";
let mailSegId = null;
function applyMailSeg(root) {
  if (!root) return;
  const tabs = root.querySelectorAll(".seg-tabs .seg-tab");
  if (!tabs.length) return;
  let target = mailSeg;
  if (![...tabs].some((t) => t.dataset.seg === target)) target = "letter";
  tabs.forEach((t) => t.classList.toggle("on", t.dataset.seg === target));
  root.querySelectorAll(".seg-pane").forEach((p) =>
    p.classList.toggle("on", p.dataset.pane === target)
  );
}
document.addEventListener("click", (event) => {
  const btn = event.target.closest(".seg-tab, [data-seg-go]");
  if (!btn) return;
  const root = btn.closest(".mail-seg");
  if (!root) return;
  mailSeg = btn.dataset.segGo || btn.dataset.seg;
  applyMailSeg(root);
});
// Re-apply the remembered tab after any settle that (re)rendered the panel.
document.body.addEventListener("htmx:afterSettle", () => {
  const root = document.querySelector("#mail-detail .mail-seg");
  if (!root) return;
  const id = root.dataset.mailId;
  if (id !== mailSegId) {
    mailSegId = id;
    mailSeg = "letter"; // a different letter opened → start on the letter tab
  }
  applyMailSeg(root);
});
// Live mini-summary in the sticky bar mirrors the wizard's client/material as
// the operator edits step 1 (hidden inputs on later steps keep the last value).
document.addEventListener("input", (event) => {
  const t = event.target;
  if (t.name !== "client_name" && t.name !== "material_color") return;
  const root = t.closest(".mail-seg");
  if (!root) return;
  const client = root.querySelector("[name=client_name]");
  const material = root.querySelector("[name=material_color]");
  const cOut = root.querySelector(".ssum-client");
  const mOut = root.querySelector(".ssum-mat");
  if (cOut && client) cOut.textContent = client.value || "—";
  if (mOut && material) mOut.textContent = material.value || "—";
});

// «Перевірити пошту» — spin + lock while the manual IMAP check runs, then a
// short cooldown (persisted in localStorage so it survives the post/redirect
// reload) keeps the button locked for a few seconds — no rapid re-spamming.
// The 2-min background auto-check is server-side and unaffected.
(function initMailSyncButton() {
  const KEY = "mailSyncAt";
  const form = document.querySelector("[data-mail-sync-form]");
  if (!form) return;
  const btn = form.querySelector(".mail-sync-btn");
  if (!btn) return;
  const label = btn.querySelector(".btn-label");
  const baseText = label ? label.textContent : "";
  const cooldownMs = (parseInt(btn.dataset.cooldown, 10) || 12) * 1000;
  let timer = null;

  function unlock() {
    if (timer) { window.clearTimeout(timer); timer = null; }
    btn.disabled = false;
    btn.classList.remove("is-cooldown", "is-syncing");
    if (label) label.textContent = baseText;
  }
  function lock(remaining) {
    btn.disabled = true;
    btn.classList.add("is-cooldown");
    (function tick() {
      if (remaining <= 0) { unlock(); return; }
      if (label) label.textContent = "Зачекайте " + Math.ceil(remaining / 1000) + " с";
      remaining -= 1000;
      timer = window.setTimeout(tick, 1000);
    })();
  }

  // Honour a cooldown left over from a recent submit (page reloaded after sync).
  const at = parseInt(localStorage.getItem(KEY) || "0", 10);
  const elapsed = Date.now() - at;
  if (at && elapsed >= 0 && elapsed < cooldownMs) lock(cooldownMs - elapsed);

  form.addEventListener("submit", (event) => {
    if (btn.disabled) { event.preventDefault(); return; } // locked → ignore
    localStorage.setItem(KEY, String(Date.now()));
    btn.classList.add("is-syncing");
    btn.disabled = true; // submission already fired; this just blocks a 2nd click
    if (label) label.textContent = "Перевіряю…";
  });
})();

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

// Клієнти, режим «Майстер»: HTMX міняє лише праву картку, тож підсвітку
// обраного в лівому списку треба перенести самим. Без цього клас `on` лишався
// на попередньому клієнті — картка показувала одного, список підсвічував
// іншого, і на списку в кілька сотень рядків оператор губив своє місце.
// Робимо це на click, а не на htmx:afterSwap, щоб підсвітка стрибала одразу,
// не чекаючи відповіді сервера.
document.addEventListener("click", (event) => {
  const item = event.target.closest("[data-cl-item]");
  if (!item) return;
  document.querySelectorAll("[data-cl-item].on").forEach((el) => el.classList.remove("on"));
  item.classList.add("on");
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
  const LS_MATSTYLE = "queueMatStyle";
  const MATSTYLES = ["pair", "code"];

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
      const on = b.getAttribute("data-density-set") === d;
      b.classList.toggle("is-active", on);
      // Стан групи має бути доступним не лише кольором — інакше для
      // клавіатури й скрінрідера перемикач мовчить.
      b.setAttribute("aria-pressed", String(on));
    });
  }

  // ---- Вигляд колонки «Матеріал / Колір» -----------------------------------
  // Та сама механіка, що й щільність: атрибут на <body>, значення в
  // localStorage, розмітка рядка НЕ змінюється. Тому перемикання не робить
  // запиту на сервер і переживає 15-секундний полл, який підмінює таблицю.
  function currentMatStyle() {
    const v = lsGet(LS_MATSTYLE);
    return MATSTYLES.indexOf(v) >= 0 ? v : "pair";
  }
  function applyMatStyle(v) {
    if (MATSTYLES.indexOf(v) < 0) v = "pair";
    // "pair" — дефолт CSS, атрибут не потрібен (тримає розмітку чистою).
    if (v === "pair") document.body.removeAttribute("data-matstyle");
    else document.body.setAttribute("data-matstyle", v);
    lsSet(LS_MATSTYLE, v);
    document.querySelectorAll("[data-matstyle-set]").forEach((b) => {
      const on = b.getAttribute("data-matstyle-set") === v;
      b.classList.toggle("is-active", on);
      b.setAttribute("aria-pressed", String(on));
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
    lsDel(LS_MATSTYLE);
    const table = getTable();
    if (table) {
      table.style.tableLayout = "";
      headCells().forEach((th) => { th.style.width = ""; });
    }
    applyDensity("normal");
    applyMatStyle("pair");
  }

  // Wire-up (no-op на сторінках без черги/кнопки).
  applySavedWidths();
  applyDensity(currentDensity());
  applyMatStyle(currentMatStyle());
  document.querySelectorAll("[data-layout-edit-toggle]").forEach((btn) => {
    btn.setAttribute("aria-pressed", String(document.body.classList.contains("layout-edit")));
    btn.addEventListener("click", () => setMode(!document.body.classList.contains("layout-edit")));
  });
  document.querySelectorAll("[data-density-set]").forEach((btn) => {
    btn.addEventListener("click", () => applyDensity(btn.getAttribute("data-density-set")));
  });
  document.querySelectorAll("[data-matstyle-set]").forEach((btn) => {
    btn.addEventListener("click", () => applyMatStyle(btn.getAttribute("data-matstyle-set")));
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

// Global toast notifications. Спливаюче повідомлення всередині CRM — щоб
// оператор бачив реальну причину помилки (напр. ukr.net відхилив вхід у пошту),
// а не мовчазний перезавантажений екран. Викликається двома шляхами:
//   1. window.showToast(text, kind) з будь-якого JS.
//   2. Автоматично, коли HTMX-відповідь несе заголовок
//      `HX-Trigger: {"toast": {"message": "...", "kind": "error"}}` — так сервер
//      підіймає тост без окремого клієнтського коду на кожен роут.
// kind: "error" | "success" | "info". Тост сам зникає; його можна закрити хрестиком.
const TOAST_ICONS = {
  error: '<path d="M12 8v5"/><path d="M12 17h.01"/><circle cx="12" cy="12" r="9"/>',
  warning: '<path d="M10.3 4.3 2.6 18a2 2 0 0 0 1.7 3h15.4a2 2 0 0 0 1.7-3L13.7 4.3a2 2 0 0 0-3.4 0Z"/><path d="M12 9v4"/><path d="M12 17h.01"/>',
  success: '<path d="m4 12.5 5 5L20 7"/>',
  info: '<path d="M12 16v-5"/><path d="M12 8h.01"/><circle cx="12" cy="12" r="9"/>',
};
// Час життя за важливістю. 0 = не зникає само: помилку, через яку стоїть
// робота, оператор мусить закрити свідомо, інакше вона згорить, поки він
// біля верстата.
const TOAST_LIFE = { error: 0, warning: 9000, info: 7000, success: 5000 };
const TOAST_MAX = 3;

function showToast(message, kind = "info", timeout, undoUrl) {
  if (!message) return;
  let stack = document.getElementById("toast-stack");
  if (!stack) {
    stack = document.createElement("div");
    stack.id = "toast-stack";
    stack.className = "toast-stack";
    stack.dataset.toastPos = "tc";
    stack.dataset.toastStyle = "glass";
    document.body.appendChild(stack);
  }
  // Стиль живе на контейнері, щоб перемикався одним атрибутом із налаштувань.
  stack.classList.remove("toast-style-glass", "toast-style-card");
  stack.classList.add("toast-style-" + (stack.dataset.toastStyle || "glass"));

  const life = timeout === undefined ? (TOAST_LIFE[kind] ?? 7000) : timeout;
  const el = document.createElement("div");
  el.className = "toast toast-" + kind + " toast-in";
  el.setAttribute("role", kind === "error" ? "alert" : "status");

  // Один рядок → лише заголовок; «Заголовок. Решта» → заголовок + пояснення.
  const split = String(message).match(/^(.{0,64}?[.!?])\s+(.+)$/s);
  const title = split ? split[1] : message;
  const rest = split ? split[2] : "";

  el.innerHTML =
    '<div class="toast-ic"><svg viewBox="0 0 24 24">' +
    (TOAST_ICONS[kind] || TOAST_ICONS.info) +
    '</svg></div><div class="toast-body"><div class="toast-title"></div>' +
    (rest ? '<div class="toast-text"></div>' : "") +
    '</div>' +
    (undoUrl ? '<button type="button" class="toast-undo">Скасувати</button>' : "") +
    '<button type="button" class="toast-close" aria-label="Закрити">×</button>' +
    (life > 0 ? '<i class="toast-life" style="animation-duration:' + life + 'ms"></i>' : "");
  el.querySelector(".toast-title").textContent = title;
  if (rest) el.querySelector(".toast-text").textContent = rest;

  const dismiss = () => {
    el.classList.remove("toast-in");
    el.classList.add("toast-out");
    window.setTimeout(() => el.remove(), 220);
  };
  el.querySelector(".toast-close").addEventListener("click", dismiss);

  // «Скасувати» — POST the undo endpoint via htmx so its own HX-Trigger toast
  // (успіх/помилка) is processed. The reverted row refreshes on the next queue
  // poll (~15s). Guard against a double-click while the request is in flight.
  const undoBtn = undoUrl && el.querySelector(".toast-undo");
  if (undoBtn) {
    undoBtn.addEventListener("click", () => {
      undoBtn.disabled = true;
      dismiss();
      if (window.htmx) {
        window.htmx.ajax("POST", undoUrl, { source: document.body, swap: "none" });
      }
    });
  }

  // Згори нові стають першими, знизу — останніми, щоб рух завжди йшов від краю.
  const pos = stack.dataset.toastPos || "tc";
  if (pos === "tc" || pos === "tr") stack.insertBefore(el, stack.firstChild);
  else stack.appendChild(el);

  if (life > 0) window.setTimeout(() => { if (el.parentNode) dismiss(); }, life);
  while (stack.children.length > TOAST_MAX) stack.firstChild.remove();
}
window.showToast = showToast;

// A "file by link" download adds an attachment and (maybe) STL files, but the
// per-row swap can't refresh the attachment list or the STL preview. The server
// fires mailFilesChanged; re-render the whole detail panel once, debounced so a
// "download all" of many links refreshes a single time after the last one. The
// active segment tab is preserved by the mail-seg afterSettle handler above.
let mailFilesRefreshTimer = null;
document.body.addEventListener("mailFilesChanged", () => {
  window.clearTimeout(mailFilesRefreshTimer);
  mailFilesRefreshTimer = window.setTimeout(() => {
    const root = document.querySelector("#mail-detail .mail-seg");
    if (!root || !window.htmx) return;
    const id = root.dataset.mailId;
    if (!id) return;
    window.htmx.ajax("GET", `/mail/${id}?panel=1`, { target: "#mail-detail", swap: "innerHTML" });
  }, 450);
});

// HTMX fires a DOM event named after each key in the response's HX-Trigger
// header. The server sends {"toast": {...}} for anything the operator must see.
document.body.addEventListener("toast", (event) => {
  const d = (event && event.detail) || {};
  showToast(d.message || d.value || "", d.kind || "info", undefined, d.undoUrl);
});

// Liquid segmented toggle for the mail-download mode. Shared by /settings and
// the /mail triage header (same markup, one handler). The endpoint blindly
// flips, so only a click on the INACTIVE side posts. The glass pill slides
// instantly (optimistic); the POST persists; on failure the state reverts so
// the UI never lies about what the server holds. No-JS falls back to nothing
// here (admin, localhost, JS always on) — the compact form used to flip on
// submit, but the animated glass toggle is JS-driven by design.
(function initDownloadToggles() {
  const segs = document.querySelectorAll("[data-dl-toggle]");
  if (!segs.length) return;

  segs.forEach((seg) => {
    seg.querySelectorAll(".dl-seg-opt").forEach((btn) => {
      btn.addEventListener("click", () => {
        const want = btn.dataset.val;
        if (want === seg.dataset.state || seg.classList.contains("is-busy")) return;

        const prev = seg.dataset.state;
        setSeg(seg, want);
        seg.classList.add("is-busy");

        fetch("/settings/mail-download/toggle", {
          method: "POST",
          headers: { "X-Requested-With": "fetch" },
        })
          .then((r) => {
            if (!(r.ok || r.status === 303)) throw new Error("HTTP " + r.status);
            if (window.showToast) {
              window.showToast(
                want === "all"
                  ? "Скачуються всі вкладення"
                  : "Скачуються лише довірені відправники",
                "success"
              );
            }
          })
          .catch(() => {
            setSeg(seg, prev);
            if (window.showToast) window.showToast("Не вдалося змінити режим", "error");
          })
          .finally(() => seg.classList.remove("is-busy"));
      });
    });
  });

  function setSeg(seg, state) {
    seg.dataset.state = state;
    seg.querySelectorAll(".dl-seg-opt").forEach((b) => {
      b.setAttribute("aria-pressed", b.dataset.val === state ? "true" : "false");
    });
    // Settings section: keep its status badge and explanatory paragraph honest.
    const sec = seg.closest(".scon-sec");
    if (sec) {
      sec.dataset.dlState = state;
      const badge = sec.querySelector(".wizard-step-head .connection-state");
      if (badge) {
        badge.textContent = state === "all" ? "Скачує всі" : "Лише довірені";
        badge.classList.toggle("connection-state-ready", state === "all");
      }
    }
  }
})();

// ── Системні тригери спливаючих сповіщень ───────────────────────────────
// Порівнюємо знімок /api/notify-state із попереднім і піднімаємо тост лише на
// ПЕРЕХОДІ (ok → error, кількість зросла). Пропущений опит нічого не «догоняє»
// — наступний просто відображає реальність, тому старий алерт не спливе двічі.
// Перелік увімкнених тригерів задається в Налаштуваннях і приїжджає в
// data-notify-events на .toast-stack.
(function initNotifyTriggers() {
  const stack = document.getElementById("toast-stack");
  if (!stack) return;
  const enabled = new Set((stack.dataset.notifyEvents || "").split(",").filter(Boolean));
  if (!enabled.size) return;

  // Follows the sync-speed preset (data-notify-poll, seconds): on Турбо the
  // "технік змінив роботу" alert lands in ~5s, not a fixed 30s. Clamped so a
  // bad value can't hammer the endpoint or stall the alert.
  const pollSec = parseInt(stack.dataset.notifyPoll, 10);
  const POLL_MS = Math.min(60, Math.max(5, Number.isFinite(pollSec) ? pollSec : 15)) * 1000;
  let prev = null;          // перший опит лише запам'ятовує базу, без тостів
  let offlineShown = false;

  function fire(event, message, kind) {
    if (enabled.has(event)) showToast(message, kind);
  }

  function plural(n, one, few, many) {
    const m10 = n % 10, m100 = n % 100;
    if (m10 === 1 && m100 !== 11) return one;
    if (m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14)) return few;
    return many;
  }

  async function poll() {
    let s;
    try {
      const r = await fetch("/api/notify-state", { headers: { "X-Requested-With": "fetch" } });
      if (!r.ok) throw new Error("HTTP " + r.status);
      s = await r.json();
    } catch (e) {
      // Застосунок не відповідає — сам себе показати він не може, тому це
      // єдиний тригер, який визначається на клієнті.
      if (!offlineShown) {
        offlineShown = true;
        fire("offline", "Втрачено зв'язок із застосунком. Дані на екрані могли застаріти — перевірте, чи працює Order Desk.", "error");
      }
      return;
    }
    if (offlineShown) {
      offlineShown = false;
      fire("sheet_recovered", "Зв'язок із застосунком відновлено.", "success");
    }

    if (prev) {
      if (prev.sheet !== "error" && s.sheet === "error") {
        fire("sheet_error", "Google Таблиця не відповідає. " + (s.sheet_label || "Черга не оновлюється."), "error");
      }
      if (prev.mail !== "error" && s.mail === "error") {
        fire("mail_error", "Пошта не відповідає. " + (s.mail_label || "Нові листи не надходять."), "error");
      }
      if ((prev.sheet === "error" && s.sheet !== "error") || (prev.mail === "error" && s.mail !== "error")) {
        fire("sheet_recovered", "Синхронізація відновлена.", "success");
      }
      if (s.orders > prev.orders) {
        const n = s.orders - prev.orders;
        fire("new_orders", n + " " + plural(n, "нова робота", "нові роботи", "нових робіт") + " у черзі.", "info");
      }
      if (s.mail_pending > prev.mail_pending) {
        const n = s.mail_pending - prev.mail_pending;
        fire("new_mail", n + " " + plural(n, "новий лист", "нові листи", "нових листів") + " у тріажі.", "info");
      }
      // Технік виправив рядок, який оператор міг уже читати. Це попередження,
      // а не інфо: фрезерувати за старою версією = брак, за який платить лаба.
      if (s.changed > prev.changed) {
        const n = s.changed - prev.changed;
        fire(
          "sheet_changed",
          n + " " + plural(n, "роботу", "роботи", "робіт") +
            " змінив технік у таблиці. Позначені в черзі — перевірте перед фрезеруванням.",
          "warning"
        );
      }
      if (s.update && s.update !== prev.update) {
        fire("update_available", "Доступне оновлення v" + s.update + ". Встановити можна в Налаштуваннях.", "warning");
      }
    }
    prev = s;
  }

  poll();
  window.setInterval(poll, POLL_MS);
})();

// ── Ліве меню: «магнітний фокус» + підказки у згорнутому режимі ─────────
// Пляма світла під курсором — це дві CSS-змінні на пункті (--mx/--my), сам
// градієнт малює ::after у base.css. Слухач один, делегований на rail, щоб не
// вішати pointermove на кожен пункт.
// Підказка — один спільний елемент на <body>: rail має overflow-y:auto, тож
// будь-який виступ убік усередині нього обрізався б.
(function initRailFocus() {
  const rail = document.querySelector(".topbar-user");
  if (!rail) return;

  rail.addEventListener("pointermove", (event) => {
    const item = event.target.closest(".rail-nav-item");
    if (!item) return;
    const r = item.getBoundingClientRect();
    item.style.setProperty("--mx", event.clientX - r.left + "px");
    item.style.setProperty("--my", event.clientY - r.top + "px");
  });

  let tip = null;
  const showTip = (item) => {
    if (!document.body.classList.contains("rail-collapsed")) return;
    const label = item.querySelector(".rail-label");
    if (!label) return;
    if (!tip) {
      tip = document.createElement("div");
      tip.className = "rail-tip";
      tip.setAttribute("role", "tooltip");
      document.body.appendChild(tip);
    }
    tip.textContent = label.textContent.trim();
    const r = item.getBoundingClientRect();
    tip.style.left = r.right + 10 + "px";
    tip.style.top = r.top + r.height / 2 + "px";
    tip.style.marginTop = "-14px";
    requestAnimationFrame(() => tip.classList.add("is-on"));
  };
  const hideTip = () => { if (tip) tip.classList.remove("is-on"); };

  rail.addEventListener("pointerover", (event) => {
    const item = event.target.closest(".rail-nav-item");
    if (item) showTip(item);
  });
  rail.addEventListener("pointerout", (event) => {
    if (!event.relatedTarget || !event.relatedTarget.closest(".rail-nav-item")) hideTip();
  });
  rail.addEventListener("focusin", (event) => {
    const item = event.target.closest(".rail-nav-item");
    if (item) showTip(item);
  });
  rail.addEventListener("focusout", hideTip);
  // Розгортання/згортання rail миттєво знімає підказку, щоб вона не «зависла».
  document.addEventListener("click", (event) => {
    if (event.target.closest("[data-rail-collapse]")) hideTip();
  });
})();
