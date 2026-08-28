// Тріаж пошти (CLAUDE.md §9, екран 2).
//
// Той самий двошаровий захист полла, що й у черзі: байт-у-байт відповідь —
// без підміни, інакше зберегти скрол списку (.listwrap). Порядок рядків під
// курсором оператора не має зсуватись — це писане правило видачі, і тут воно
// діє так само.

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
