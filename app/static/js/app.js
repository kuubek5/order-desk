// Оболонка застосунку: те, що є на КОЖНІЙ сторінці.
//
// Тости, спливаючі сповіщення, ліве меню, оверлей оновлення, копіювання в
// буфер і «Відкрити папку». Усі обробники делеговані на document, тому вони
// живі й після HTMX-підміни, і мовчазні на сторінках, де такої розмітки немає.
//
// Завантажується ПЕРШИМ (див. base.html): showToast і window.showToast
// оголошені тут, а решта екранних файлів на них розраховує.

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

// «Відкрити папку» на картці клієнта (видача). Кнопка лишається звичайним
// <a href="file://...">, але ЗВИЧАЙНИЙ клік по ньому браузер зі сторінки на
// http блокує мовчки — саме тому кнопка не робила нічого (бойовий випадок
// 28.08.26). Тому клік перехоплюємо й просимо відкрити Провідник сервер, як
// це вже роблять прев'ю STL і подвійний клік у черзі.
//
// Мовчазна кнопка — гірше за зламану: якщо не вийшло, оператор мусить це
// бачити, а не гадати, чи він узагалі влучив.
document.addEventListener("click", (event) => {
  const button = event.target.closest("[data-open-folder-token]");
  if (!button) return;

  event.preventDefault();
  const token = button.dataset.openFolderToken || "";
  if (!token) return;

  button.classList.add("is-opening");
  fetch("/open-folder", {
    method: "POST",
    body: new URLSearchParams({ token: token }),
    credentials: "same-origin",
  })
    .then((response) => {
      if (!response.ok) throw new Error(String(response.status));
    })
    .catch(() => {
      if (window.showToast) {
        window.showToast("Не вдалося відкрити папку — перевірте доступ до сховища", "error");
      }
    })
    .finally(() => {
      button.classList.remove("is-opening");
    });
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
    // Тости несуть єдиний зворотний зв'язок для дій, які нічого не міняють на
    // сторінці (запис у таблицю, скасування, помилка синку). Без aria-live
    // вони не існують для зчитувача екрана взагалі.
    stack.setAttribute("role", "status");
    stack.setAttribute("aria-live", "polite");
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
