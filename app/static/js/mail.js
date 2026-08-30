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
function markMailRowActive(event) {
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
}

document.addEventListener("click", markMailRowActive);
// Рядок відкривається ще й з клавіатури (hx-trigger keyup Enter/Space у
// _mail_triage_list.html), а підсвітка жила лише на кліку: Tab+Enter відкривав
// лист, але «активним» лишався попередній рядок і крапка «непрочитано» не
// гасла — оператор не бачив, який лист він читає.
document.addEventListener("keyup", (event) => {
  if (event.key !== "Enter" && event.key !== " ") return;
  markMailRowActive(event);
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

// ── Шестерня «вигляд списку» ─────────────────────────────────────────────
// Відступ рядка й ширина панелі списку — на акаунті оператора, не в
// localStorage: набір їде за ним на будь-який браузер цього ПК і повертається
// при наступному вході. Сервер уже підставив стартові значення в data-* і в
// style на <main>, тому цей код нічого не «вмикає» — він лише крутить далі.
(() => {
  const root = document.querySelector("[data-mail-look]");
  if (!root) return;
  const main = document.querySelector("main.mailv2");
  const panel = root.querySelector("#look-panel");
  const toggle = root.querySelector("[data-look-toggle]");
  if (!main || !panel || !toggle) return;

  // Ті самі межі, що на сервері (app/routers/mail.py). Дублюються свідомо:
  // кнопка мусить гаснути на краю ОДРАЗУ, а не після відповіді мережі.
  // Розійтись вони не можуть тихо — є тест-сторож.
  const LIMITS = { pad: [2, 28], width: [340, 1180] };
  // Значення, з яких починає лічильник, якщо оператор ще нічого не крутив.
  // Мусять збігатися з дефолтами CSS-змінних у v2a_mail.css.
  const FALLBACK = { pad: 6, width: 0 };

  const state = {
    pad: parseInt(root.dataset.pad || "0", 10) || 0,
    width: parseInt(root.dataset.width || "0", 10) || 0,
    step: parseInt(root.dataset.step || "2", 10) || 2,
  };

  // Ширина «0» означає автоматичну (clamp за шириною екрана). Щоб перше
  // клацання по «−» не стрибнуло від нуля до мінімуму, стартуємо від того,
  // що зараз реально на екрані.
  const currentWidth = () =>
    state.width || Math.round(root.closest(".mailv2").querySelector(".list-panel").getBoundingClientRect().width);

  function apply() {
    main.style.setProperty("--mail-row-pad", (state.pad || FALLBACK.pad) + "px");
    if (state.width) main.style.setProperty("--mail-list-w", state.width + "px");
    else main.style.removeProperty("--mail-list-w");
    render();
  }

  function render() {
    const padOut = root.querySelector('[data-look-out="pad"]');
    const widthOut = root.querySelector('[data-look-out="width"]');
    if (padOut) padOut.textContent = (state.pad || FALLBACK.pad) + " px";
    if (widthOut) widthOut.textContent = state.width ? state.width + " px" : "авто";
    root.querySelectorAll("[data-look-step]").forEach((btn) => {
      btn.setAttribute("aria-pressed", String(parseInt(btn.dataset.lookStep, 10) === state.step));
    });
    // Кнопка на краю діапазону гасне — інакше вона мовчки нічого не робить,
    // і оператор клацає далі, думаючи, що зламалось.
    ["pad", "width"].forEach((key) => {
      const value = key === "pad" ? state.pad || FALLBACK.pad : state.width || 0;
      const [low, high] = LIMITS[key];
      const dec = root.querySelector('[data-look-dec="' + key + '"]');
      const inc = root.querySelector('[data-look-inc="' + key + '"]');
      if (dec) dec.disabled = value !== 0 && value <= low;
      if (inc) inc.disabled = value !== 0 && value >= high;
    });
  }

  let saveTimer = null;
  function save() {
    // Серія клацань по «+» — це одна зміна, а не вісім: чекаємо, поки рука
    // зупиниться, і тоді пишемо один раз.
    window.clearTimeout(saveTimer);
    saveTimer = window.setTimeout(() => {
      const body = new URLSearchParams({
        row_pad: String(state.pad),
        list_width: String(state.width),
        step: String(state.step),
      });
      fetch("/mail/prefs", {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: body.toString(),
        credentials: "same-origin",
      }).then((response) => {
        if (!response.ok && window.showToast) {
          window.showToast("Не вдалось зберегти вигляд списку", "error");
        }
      }).catch(() => {
        if (window.showToast) window.showToast("Не вдалось зберегти вигляд списку", "error");
      });
    }, 400);
  }

  function nudge(key, direction) {
    const [low, high] = LIMITS[key];
    const base = key === "pad" ? state.pad || FALLBACK.pad : currentWidth();
    const next = Math.max(low, Math.min(high, base + direction * state.step));
    state[key] = next;
    apply();
    save();
  }

  root.addEventListener("click", (event) => {
    const dec = event.target.closest("[data-look-dec]");
    if (dec) { nudge(dec.dataset.lookDec, -1); return; }
    const inc = event.target.closest("[data-look-inc]");
    if (inc) { nudge(inc.dataset.lookInc, 1); return; }
    const stepBtn = event.target.closest("[data-look-step]");
    if (stepBtn) {
      state.step = parseInt(stepBtn.dataset.lookStep, 10) || 2;
      render();
      save();
      return;
    }
    if (event.target.closest("[data-look-reset]")) {
      state.pad = 0;
      state.width = 0;
      main.style.removeProperty("--mail-row-pad");
      main.style.removeProperty("--mail-list-w");
      render();
      save();
      return;
    }
    if (event.target.closest("[data-look-toggle]")) {
      const open = panel.hidden;
      panel.hidden = !open;
      toggle.setAttribute("aria-expanded", String(open));
    }
  });

  // Клік повз панель і Esc закривають її — інакше вона накриває верхні рядки
  // списку доти, доки оператор не здогадається клацнути по шестерні ще раз.
  document.addEventListener("click", (event) => {
    if (panel.hidden || root.contains(event.target)) return;
    panel.hidden = true;
    toggle.setAttribute("aria-expanded", "false");
  });
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape" || panel.hidden) return;
    panel.hidden = true;
    toggle.setAttribute("aria-expanded", "false");
    toggle.focus();
  });

  render();
})();
