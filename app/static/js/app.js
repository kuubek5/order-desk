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

    // Вигадані стадії по таймеру — лише ПОКИ сервер не віддав справжній стан.
    // Через проксі лабораторії 45 МБ качаються хвилинами; таймер добігав до
    // «Перезапуск… за мить» за 9 с, і далі оператор дивився на нерухомий
    // напис («зависло»). Щойно /settings/update/status відповів — таймер
    // зупиняється, і напис каже, що відбувається насправді.
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
    startInstallStatusPoll(overlay, statusEl);
  }

  function stopStageCycling() {
    if (stageTimer) {
      window.clearInterval(stageTimer);
      stageTimer = null;
    }
  }

  function megabytes(bytes) {
    return (bytes / 1048576).toFixed(bytes < 10485760 ? 1 : 0);
  }

  function describeInstallState(state) {
    switch (state.stage) {
      case "downloading":
        if (state.total > 0) {
          return "Завантаження… " + megabytes(state.done) + " з " + megabytes(state.total) + " МБ";
        }
        return "Завантаження… " + megabytes(state.done || 0) + " МБ";
      case "launching":
        return "Перевірено, запуск інсталятора…";
      case "launched":
        return "Перезапуск… за мить сторінка оновиться";
      case "failed":
        return "Не вдалося: " + (state.message || "невідома причина");
      default:
        return null;
    }
  }

  // Справжній стан з сервера раз на секунду. Коли сервер падає на перезапуск,
  // запити ламаються — це нормально, далі веде health-полл.
  let statusTimer = null;
  function startInstallStatusPoll(overlay, statusEl) {
    const track = overlay.querySelector(".update-track");
    const bar = track ? track.querySelector("i") : null;
    const closeBtn = document.getElementById("update-close");
    statusTimer = window.setInterval(() => {
      fetch("/settings/update/status", { cache: "no-store", credentials: "same-origin" })
        .then((r) => (r.ok ? r.json() : null))
        .then((state) => {
          if (!state || state.stage === "idle") return;
          const text = describeInstallState(state);
          if (!text) return;
          stopStageCycling();
          if (statusEl) statusEl.textContent = text;
          if (track && bar && state.stage === "downloading" && state.total > 0) {
            track.classList.add("is-real");
            bar.style.setProperty("--w", Math.round((state.done / state.total) * 100) + "%");
          }
          if (state.stage === "failed") {
            window.clearInterval(statusTimer);
            statusTimer = null;
            if (healthTimer) {
              window.clearInterval(healthTimer);
              healthTimer = null;
            }
            overlay.classList.add("is-failed");
            if (closeBtn) closeBtn.hidden = false;
          }
        })
        .catch(() => {});
    }, 1000);
  }

  function hideUpdateOverlay() {
    const overlay = document.getElementById("update-overlay");
    if (!overlay) return;
    if (statusTimer) {
      window.clearInterval(statusTimer);
      statusTimer = null;
    }
    stopStageCycling();
    overlay.classList.remove("is-shown", "is-failed");
    overlay.hidden = true;
    overlay.setAttribute("aria-hidden", "true");
    const closeBtn = document.getElementById("update-close");
    if (closeBtn) closeBtn.hidden = true;
    document
      .querySelectorAll('form[action="/settings/update/install"] button[type="submit"]')
      .forEach((b) => {
        b.disabled = false;
      });
    shown = false;
  }

  document.addEventListener("click", (event) => {
    if (event.target.closest("#update-close")) hideUpdateOverlay();
  });

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

    // Один клік перезапускав застосунок посеред зміни на СПІЛЬНОМУ цеховому
    // ПК: колега міг саме приймати лист або вести видачу (аудит 05.09.26,
    // UX 1.10). Питаємо один раз, і кажемо, кого це зачепить. Число готує
    // сервер (data-busy) — це оператори, що щось робили за останні пів
    // години, а не «онлайн»: таблиці сесій у застосунку немає.
    const busy = parseInt(form.dataset.busy || "0", 10) || 0;
    const who = busy > 1
      ? `Зараз працюють ще ${busy - 1} — їхню роботу обірве.`
      : "";
    if (!window.confirm(
      "Встановити оновлення? Застосунок перезапуститься, і всі відкриті " +
      "екрани оновляться. " + who
    )) {
      return;
    }

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
  // KMStore сам ковтає приватний режим і сам чіпляє префікс kuubmill:v1:.
  KMStore.set("railCollapsed", collapsed ? "1" : "0");
  btn.setAttribute("aria-label", collapsed ? "Розгорнути меню" : "Згорнути меню");
  btn.setAttribute("title", collapsed ? "Розгорнути меню" : "Згорнути меню");
});

// Віджет 3D-друку над чергою: чіп розгортається в табло.
//
// Стан живе КЛАСОМ НА BODY, а не всередині віджета: сам віджет свапається
// поллом кожні 15 с (hx-swap="outerHTML"), і будь-який стан у ньому згортався
// б під рукою оператора. Той самий урок, що зі смугою печей.
//
// Делегований слухач — з тієї ж причини: після свапу кнопка в DOM уже інша,
// і прямий addEventListener на неї перестав би працювати мовчки.
document.addEventListener("click", (event) => {
  const btn = event.target.closest("[data-sisma-toggle]");
  if (!btn) return;
  const open = document.body.classList.toggle("sisma-open");
  KMStore.set("sismaOpen", open ? "1" : "0");
  syncSismaToggles();
});

// aria-expanded мусить оновитись і після полла, і при відновленні стану з
// localStorage — тому це окрема функція, а не рядок усередині обробника.
function syncSismaToggles() {
  const open = document.body.classList.contains("sisma-open");
  document.querySelectorAll("[data-sisma-toggle]").forEach((btn) => {
    btn.setAttribute("aria-expanded", String(open));
    btn.setAttribute("title", open ? "Згорнути" : "Показати шари й час завершення");
  });
}

document.addEventListener("DOMContentLoaded", () => {
  if (KMStore.get("sismaOpen") === "1") document.body.classList.add("sisma-open");
  syncSismaToggles();
});
// Після полла кнопки в DOM нові — повертаємо їм правильний aria-стан.
document.body.addEventListener("htmx:afterSwap", (event) => {
  if (event.target && event.target.id === "sisma-strip") syncSismaToggles();
});

// ── Вкладки розділу в рейці налаштувань ───────────────────────────────────
// Частина розділів має всередині вкладки («Копії таблиці» в Google Таблиці,
// «Скачування вкладень» у Пошті, «Сповіщення» в кабінеті). У рейці вони
// стоять під своїм господарем і ЗГОРНУТІ: меню не повинно розповідати про
// внутрішній устрій розділу тому, хто його зараз не відкриває.
//
// Розкриває їх сам господар (клік по ньому і відкриває розділ, і показує
// вкладки) або каретка поруч — коли треба лише зазирнути, нікуди не йдучи.
// Стан памʼятається, тому оператор, який живе в «Копіях», не розкриває їх
// щоразу заново.
(function initRailSubs() {
  const groups = document.querySelectorAll("[data-subs-of]");
  if (!groups.length) return;

  function parentOf(key) {
    return document.querySelector('[data-subparent="' + CSS.escape(key) + '"]');
  }

  function setOpen(key, open) {
    const box = document.querySelector('[data-subs-of="' + CSS.escape(key) + '"]');
    if (!box) return;
    box.hidden = !open;
    const head = parentOf(key);
    if (head) {
      head.classList.toggle("is-subs-open", open);
      const caret = head.querySelector("[data-subtoggle]");
      if (caret) caret.setAttribute("aria-expanded", open ? "true" : "false");
    }
  }

  // Відкриваємо самі, коли активний пункт живе в цій групі — інакше людина
  // бачила б підсвічений розділ і жодного натяку, ЯКА саме вкладка відкрита.
  // Стан НЕ памʼятається між заходами: типово згорнуто. Єдиний виняток —
  // група, всередині якої стоїть активний пункт: інакше людина бачила б
  // підсвічений розділ і жодного натяку, ЯКА саме вкладка зараз відкрита.
  groups.forEach((box) => {
    const key = box.dataset.subsOf;
    const head = parentOf(key);
    const open =
      !!box.querySelector(".is-active") ||
      !!(head && head.querySelector(".rail-nav-item.is-active"));
    setOpen(key, open);
  });

  document.addEventListener("click", (event) => {
    const caret = event.target.closest("[data-subtoggle]");
    if (caret) {
      // Каретка НЕ веде в розділ: це «зазирнути», а не «перейти».
      event.preventDefault();
      event.stopPropagation();
      const key = caret.dataset.subtoggle;
      const box = document.querySelector('[data-subs-of="' + CSS.escape(key) + '"]');
      setOpen(key, box ? box.hidden : true);
      return;
    }
    const head = event.target.closest("[data-subparent]");
    if (head) setOpen(head.dataset.subparent, true);
  });

  // Свій розділ відкрили не мишею (палітра Ctrl+K, #hash) — рейку теж треба
  // розгорнути, щоб активна вкладка була видима.
  window.railSubsReveal = function (key) {
    const item = document.querySelector('.rail-nav-item[data-sec="' + CSS.escape(key) + '"]');
    const box = item && item.closest("[data-subs-of]");
    if (box) setOpen(box.dataset.subsOf, true);
  };
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

// ── «Аврора»: три канали під три роди подій ─────────────────────────────
// Вигляд «aurora» (Налаштування → Сповіщення) не просто перефарбовує тост, а
// розводить повідомлення по трьох каналах — рішення власника 07.09.26:
//   card  — подія прилетіла ЗЗОВНІ або дія не долетіла в таблицю;
//   line  — підтвердження ВЛАСНОЇ дії (оператор дивиться на кнопку, яку
//           щойно натиснув, тож досить рядка знизу на 2 с);
//   edge  — СТАН збою: він триває, а не стався, тому липкий тост тут зайвий.
// Вигляди «glass» і «card» лишились як були: там усе йде однією колонкою.
const AURORA_EVENT_CHANNEL = {
  offline: "edge",
  sheet_error: "edge",
  mail_error: "edge",
  sheet_recovered: "edge",
};
// Скільки живе рядок-підтвердження. Окремо від TOAST_LIFE: рядок не несе
// подробиць, його завдання — сказати «дійшло» і піти.
const TOAST_LINE_LIFE = 2600;
const TOAST_LINE_MAX = 2;

function toastStackEl() {
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
  return stack;
}

function toastStyleName() {
  const stack = document.getElementById("toast-stack");
  return (stack && stack.dataset.toastStyle) || "glass";
}

// Куди відправити це повідомлення. Явний opts.channel > канал події >
// правило за важливістю. Помилка власної дії свідомо лишається КАРТКОЮ:
// «у таблицю НЕ записано» — найдорожче повідомлення в системі, рядок унизу
// його б поховав.
function auroraChannel(kind, opts) {
  if (toastStyleName() !== "aurora") return "card";
  if (opts && opts.channel) return opts.channel;
  if (opts && opts.event) return AURORA_EVENT_CHANNEL[opts.event] || "card";
  return kind === "error" || kind === "warning" ? "card" : "line";
}

// Нижня стрічка. Один рядок, без подробиць, гасне сам; «Скасувати» лишається,
// бо це єдина дія, яку з підтвердження власної дії справді хочуть.
function showToastLine(message, kind, undoUrl) {
  let zone = document.getElementById("toast-lines");
  if (!zone) {
    zone = document.createElement("div");
    zone.id = "toast-lines";
    zone.className = "toast-lines";
    zone.setAttribute("role", "status");
    zone.setAttribute("aria-live", "polite");
    document.body.appendChild(zone);
  }
  const el = document.createElement("div");
  el.className = "toast-line toast-line-" + kind + " toast-line-in";
  el.innerHTML =
    '<span class="tl-dot" aria-hidden="true"></span><span class="tl-text"></span>' +
    (undoUrl ? '<button type="button" class="tl-undo">Скасувати</button>' : "") +
    '<button type="button" class="tl-close" aria-label="Закрити">×</button>' +
    '<i class="tl-thread" style="animation-duration:' + TOAST_LINE_LIFE + 'ms"></i>';
  el.querySelector(".tl-text").textContent = String(message);

  // Клас на <body> — щоб кнопка звернень (той самий нижній кут) піднялась над
  // стрічкою. Явно, а не через CSS `body:has(.toast-line)`: той теж працює, але
  // змусив би браузер перевіряти умову на кожній мутації під body, а черга
  // підмінює сотні рядків кожні 15 с.
  const syncBodyFlag = () => {
    document.body.classList.toggle("has-toast-line", zone.children.length > 0);
  };
  const dismiss = () => {
    el.classList.remove("toast-line-in");
    el.classList.add("toast-line-out");
    window.setTimeout(() => { el.remove(); syncBodyFlag(); }, 220);
  };
  el.querySelector(".tl-close").addEventListener("click", dismiss);
  const undoBtn = undoUrl && el.querySelector(".tl-undo");
  if (undoBtn) {
    undoBtn.addEventListener("click", () => {
      undoBtn.disabled = true;
      dismiss();
      if (window.htmx) {
        window.htmx.ajax("POST", undoUrl, { source: document.body, swap: "none" });
      }
    });
  }
  zone.appendChild(el);
  while (zone.children.length > TOAST_LINE_MAX) zone.firstChild.remove();
  syncBodyFlag();
  window.setTimeout(() => { if (el.parentNode) dismiss(); }, TOAST_LINE_LIFE);
}

// Світна кромка + чіп. Кромка живе, доки живий бодай один збій; чіп показує,
// який саме, і віддає подробиці при наведенні. «Відновлено» гасить обидва.
const auroraFaults = new Map();
// Один таймер гасіння на всю кромку. Без нього кожне «відновлено» лишало по
// власному таймеру, і СТАРИЙ гасив зелене підтвердження раніше, ніж мало
// згаснути нове: у послідовності збій → відновлено → збій → відновлено
// зелений чіп зникав за секунду замість 2.6 (перевірено наживо 08.09.26).
let _auroraHideTimer = null;
function showToastEdge(message, kind, event) {
  let edge = document.getElementById("toast-edge");
  let chip = document.getElementById("toast-chip");
  if (!edge) {
    edge = document.createElement("div");
    edge.id = "toast-edge";
    edge.className = "toast-edge";
    document.body.appendChild(edge);
  }
  if (!chip) {
    chip = document.createElement("div");
    chip.id = "toast-chip";
    chip.className = "toast-chip";
    chip.setAttribute("role", "status");
    chip.setAttribute("aria-live", "polite");
    chip.setAttribute("tabindex", "0");
    chip.innerHTML =
      '<span class="tc-dot" aria-hidden="true"></span><span class="tc-name"></span>' +
      '<span class="tc-more"></span><div class="toast-chip-detail"></div>';
    document.body.appendChild(chip);
  }

  const recovered = kind === "success" || event === "sheet_recovered";
  if (recovered) {
    auroraFaults.clear();
  } else {
    auroraFaults.set(event || "fault", { message: String(message), at: new Date() });
  }

  const short = {
    sheet_error: "Таблиця",
    mail_error: "Пошта",
    offline: "Зв'язок",
  };
  const keys = [...auroraFaults.keys()];
  edge.classList.toggle("toast-edge-ok", recovered);
  chip.classList.toggle("toast-chip-ok", recovered);
  if (recovered) {
    chip.querySelector(".tc-name").textContent = "Зв'язок відновлено";
    chip.querySelector(".tc-more").textContent = "";
    chip.querySelector(".toast-chip-detail").textContent = String(message);
    edge.classList.add("is-on");
    chip.classList.add("is-on");
    document.body.classList.add("has-toast-chip");
    window.clearTimeout(_auroraHideTimer);
    _auroraHideTimer = window.setTimeout(() => {
      // Гасимо лише якщо за цей час не прилетів новий збій.
      if (!auroraFaults.size) {
        edge.classList.remove("is-on");
        chip.classList.remove("is-on");
        document.body.classList.remove("has-toast-chip");
      }
    }, 2600);
    return;
  }
  // Збій не гасне сам — знімаємо чужий таймер гасіння, якщо він лишився
  // від попереднього «відновлено».
  window.clearTimeout(_auroraHideTimer);
  _auroraHideTimer = null;
  const first = keys[0];
  const since = auroraFaults.get(first).at;
  const hh = String(since.getHours()).padStart(2, "0");
  const mm = String(since.getMinutes()).padStart(2, "0");
  chip.querySelector(".tc-name").textContent =
    (short[first] || "Збій") + (keys.length > 1 ? " +" + (keys.length - 1) : "");
  chip.querySelector(".tc-more").textContent = "з " + hh + ":" + mm;
  chip.querySelector(".toast-chip-detail").textContent = keys
    .map((k) => auroraFaults.get(k).message)
    .join(" · ");
  edge.classList.add("is-on");
  chip.classList.add("is-on");
  // Клас на <body> зсуває верхні тости нижче чіпа. Явно, не через :has() —
  // див. коментар у update_overlay.css біля .has-toast-line.
  document.body.classList.add("has-toast-chip");
}

function showToast(message, kind = "info", timeout, undoUrl, opts) {
  if (!message) return;
  const stack = toastStackEl();
  // Стиль живе на контейнері, щоб перемикався одним атрибутом із налаштувань.
  stack.classList.remove("toast-style-glass", "toast-style-card", "toast-style-aurora");
  stack.classList.add("toast-style-" + (stack.dataset.toastStyle || "glass"));

  const channel = auroraChannel(kind, opts);
  if (channel === "line") return showToastLine(message, kind, undoUrl);
  if (channel === "edge") return showToastEdge(message, kind, opts && opts.event);

  const life = timeout === undefined ? (TOAST_LIFE[kind] ?? 7000) : timeout;
  const el = document.createElement("div");
  el.className = "toast toast-" + kind + " toast-in";
  el.setAttribute("role", kind === "error" ? "alert" : "status");

  // Один рядок → лише заголовок; «Заголовок. Решта» → заголовок + пояснення.
  const split = String(message).match(/^(.{0,64}?[.!?])\s+(.+)$/s);
  const title = split ? split[1] : message;
  const rest = split ? split[2] : "";

  // В «Аврорі» час життя показує кільце довкола іконки, а не смужка внизу:
  // картка тоді не має «дна», яке з'їдає рух, і залишок видно біля глифа.
  const ring =
    toastStyleName() === "aurora" && life > 0
      ? '<svg class="toast-ring" viewBox="0 0 40 40" aria-hidden="true">' +
        '<circle class="tr-bg"/><circle class="tr-fg" style="animation-duration:' + life + 'ms"/></svg>'
      : "";
  el.innerHTML =
    '<div class="toast-ic">' + ring + '<svg class="toast-glyph" viewBox="0 0 24 24">' +
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

// Дедлайн на HTMX-запит.
//
// htmx лишає `xhr.timeout` нулем — тобто «чекати стільки, скільки віддасть
// ОС». На обірваному Wi-Fi це хвилини: кнопка сидить у htmx-request, полл
// тримає зʼєднання, і жоден наш обробник помилки не спрацьовує, бо помилки
// формально ще немає. Другий оператор ходитиме по мережі, тож ця тиша стає
// щоденною. Звичайний запит у нас — локальна база й десяті долі секунди, тому
// 10 с вистачає з великим запасом, а після них htmx кине htmx:timeout і рука
// звільниться.
const HTMX_TIMEOUT_MS = 10000;

// Виняток — запити, довгі ЗА ПРИРОДОЮ, а не через мережу: перевірки звʼязку в
// налаштуваннях (IMAP, доступ до таблиці, проба мережевого шляху), OAuth (сам
// сервер чекає на згоду адміна до 180 с), скачування й розпакування вкладень,
// знімок з обладнання, обхід export по SMB. Дедлайн їм лишаємо теж, але вже
// не «розумний», а страхувальний: він рятує від вічного зависання, не рубаючи
// живу роботу. Новий довгий роут — сюди, інакше він обірветься на 10-й секунді
// з тостом про помилку.
const HTMX_SLOW_TIMEOUT_MS = 300000;
const HTMX_SLOW_PATHS = [
  // Увесь екран налаштувань: він адмінський і рідкісний, а майже кожна його
  // кнопка кудись стукає — дешевше дати запас цілому розділу, ніж вгадувати
  // поіменно, яка перевірка сьогодні довша за десять секунд.
  /^\/settings(?:\/|$)/,
  /^\/mail\/\d+\/(?:download-attachments|redownload|fetch-link|extract-archives|accept)$/,
  /^\/(?:furnaces|machines)\/refresh$/,
  /^\/handout\/cards$/,
  /^\/vyrobitok\/day-sync$/,
  // Відкриття теки в Провіднику: шлях — мережева шара, і мертвий хост
  // відповідає не швидше за ОС.
  /\/(?:open-)?folder$/,
];

if (window.htmx) window.htmx.config.timeout = HTMX_TIMEOUT_MS;

document.body.addEventListener("htmx:configRequest", (event) => {
  const detail = event.detail || {};
  const path = String(detail.path || "").split("?")[0];
  if (HTMX_SLOW_PATHS.some((re) => re.test(path))) {
    detail.timeout = HTMX_SLOW_TIMEOUT_MS;
  }
});

// Спрацьований дедлайн мусить бути чутним рівно так само, як 5xx нижче: htmx
// кидає на нього ОКРЕМУ подію (не responseError і не sendError), тож без цього
// обробника кнопка мовчала б — та сама пастка, від якої лікує наступний блок.
document.body.addEventListener("htmx:timeout", (event) => {
  const elt = event.detail && event.detail.elt;
  const trigger = (elt && elt.getAttribute && elt.getAttribute("hx-trigger")) || "";
  // Фоновий полл — мовчки: у нього вже є свій сигнал (перевірка пульсу), а
  // тост кожні 15 с перетворив би екран на стрічку однакових повідомлень.
  if (/every\s/.test(trigger)) return;
  showToast("Сервер не відповів вчасно — дію не виконано", "error");
});

// Провал HTMX-запиту мусить бути ЧУТНИМ.
//
// Сторінка помилки свідомо не віддається на HX-Request (вона зламала б
// розмітку, вставившись у середину таблиці) — але заміни в неї не було, тобто
// на черзі й у тріажі, де майже все ходить через HTMX, 500 не показував
// НІЧОГО. Оператор тисне «Взяти», сервер падає, кнопка мовчить. Це рівно та
// пастка, яку в цьому проєкті вже ловили з тихим провалом запису в таблицю:
// мовчазна помилка гірша за гучну, бо робота лишається «можна брати» для всіх
// інших і піде в повторне фрезерування.
document.body.addEventListener("htmx:responseError", (event) => {
  const detail = event.detail || {};
  const status = (detail.xhr && detail.xhr.status) || 0;
  // Фонові полли (черга кожні 15 с, смуга й секція печей — 30 с) НЕ мають
  // права нікуди вести й нічого показувати. Інакше одна протухла сесія
  // викидала б оператора зі сторінки посеред видачі — сам, без жодної його
  // дії, і з утраченим місцем у списку. Дію від полла відрізняємо за тим, що
  // полл ходить по таймеру: у htmx це `every …s` у hx-trigger.
  const elt = detail.elt;
  const trigger = (elt && elt.getAttribute && elt.getAttribute("hx-trigger")) || "";
  if (/every\s/.test(trigger)) return;

  // 401 — сесія скінчилась; тост тут нічого не дає, треба на вхід.
  if (status === 401) { window.location.href = "/login"; return; }
  const what = status ? " (код " + status + ")" : "";
  showToast("Дію не виконано" + what + " — спробуйте ще раз", "error");
});

// Мережа обірвалась посеред дії — окремо від 5xx: тут винен не застосунок, і
// порада інша.
document.body.addEventListener("htmx:sendError", (event) => {
  // Та сама межа: обрив зв'язку на фоновому поллі вже має свій сигнал —
  // тост «Втрачено зв'язок» від перевірки пульсу. Другий тост кожні 15 секунд
  // перетворив би екран на стрічку однакових повідомлень.
  const elt = event.detail && event.detail.elt;
  const trigger = (elt && elt.getAttribute && elt.getAttribute("hx-trigger")) || "";
  if (/every\s/.test(trigger)) return;
  showToast("Немає зв'язку із застосунком — дію не виконано", "error");
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

  // Ключ події їде далі у showToast: у вигляді «Аврора» він вирішує канал
  // (збої → кромка, решта → картка). Гейт лишається тут і тільки тут —
  // вимкнена в Налаштуваннях подія не з'явиться в жодному каналі.
  function fire(event, message, kind) {
    if (enabled.has(event)) showToast(message, kind, undefined, undefined, { event });
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
      // 401/403 — це «ще не увійшли», а не «застосунок упав». Без цієї гілки
      // сторінка входу сама собі повідомляла, що зв'язок втрачено: сервер
      // живий, просто сесії ще немає. Опитування там і не потрібне — нема
      // чого сповіщати, поки нема оператора.
      if (r.status === 401 || r.status === 403) {
        stop();
        return;
      }
      if (!r.ok) throw new Error("HTTP " + r.status);
      s = await r.json();
    } catch (e) {
      // Застосунок не відповідає — сам себе показати він не може, тому це
      // єдиний тригер, який визначається на клієнті.
      if (!offlineShown) {
        offlineShown = true;
        fire("offline", "Втрачено зв'язок із застосунком. Дані на екрані могли застаріти — перевірте, чи працює KuubMill.", "error");
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
      // Тост «нові роботи» СВІДОМО вимкнено (05.09.26): s.orders — це РОЗМІР
      // черги (status != видано), тож повернення «видано»→«нове» (повернули
      // синю заливку / зняли галочку) роздувало його й показувало неіснуючі
      // «нові роботи». Повернути можна разом із фіксом підрахунку — коли
      // orders рахуватиме появу роботи, а не розмір черги. Подію також прибрано
      // зі списку в settings_store.NOTIFY_EVENTS.
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
      // Записка передачі зміни. Чесно про межу: `prev` живе в пам'яті вкладки,
      // тож тост спрацює лише якщо записку написали, поки застосунок уже
      // відкритий. Сценарій «прийшов о 08:00, відкрив свіжу сторінку» він не
      // покриває за побудовою — там працюють картка на черзі й бейдж у рейці.
      if (s.shift > prev.shift) {
        const n = s.shift - prev.shift;
        fire(
          "shift",
          n + " " + plural(n, "нова записка", "нові записки", "нових записок") +
            " передачі зміни. Відкрийте «Зміна».",
          "warning"
        );
      }
      // «Можна брати»: технік доклав шлях до папки. Рахуємо СТАН готовності
      // (job_code є, Sum3D порожній), а не розмір черги — саме через розмір
      // старий `new_orders` показував роботи, яких немає, і був прибраний.
      if (s.ready > prev.ready) {
        const n = s.ready - prev.ready;
        fire(
          "ready_to_take",
          n + " " + plural(n, "роботу", "роботи", "робіт") + " можна брати — технік доклав шлях до папки.",
          "info"
        );
      }
      // Рядок зник із таблиці. archived_at ставить лише синк, коли рядка не
      // знайшлось; retention чергу лише фільтрує за датою і нічого не штампує.
      if (s.deleted > prev.deleted) {
        const n = s.deleted - prev.deleted;
        fire(
          "row_deleted",
          n + " " + plural(n, "рядок", "рядки", "рядків") + " " +
            plural(n, "зник", "зникли", "зникло") + " з таблиці. " +
            plural(n, "Робота перейшла", "Роботи перейшли", "Роботи перейшли") + " в Архів, файли на місці.",
          "warning"
        );
      }
      if (s.update && s.update !== prev.update) {
        fire("update_available", "Доступне оновлення v" + s.update + ". Встановити можна в Налаштуваннях.", "warning");
      }
    }
    prev = s;
  }

  let timer = null;
  function stop() {
    if (timer !== null) {
      window.clearInterval(timer);
      timer = null;
    }
  }

  poll();
  timer = window.setInterval(poll, POLL_MS);
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
