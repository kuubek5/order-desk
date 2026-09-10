// Видача (CLAUDE.md §9.4).
//
// Перемикач «Рядки / Плитки» звідси прибрано (07.09.26): плитками не
// користувались, а два схожі перемикачі поспіль у шапці плутались між
// собою — клік по сусідньому скидав режим.
// ── Перемикач розкладки: «Список» ⇄ «Список + покажчик» ─────────────────
// Вибір зберігається на АКАУНТІ (POST /account/look, scope=handout), а не в
// localStorage: розкладка їде за оператором на будь-який браузер цього ПК і
// повертається при наступному вході — так само, як теми й шестерня черги.
//
// Після збереження сторінка перезавантажується цілком. Розкладка міняє каркас
// НАВКОЛО списку (з'являється друга колонка), і підміняти половину каркаса
// заради економії пів секунди означало б плодити стани, у яких видача
// виглядає наполовину так, наполовину інакше.
document.addEventListener("click", (event) => {
  const btn = event.target.closest("[data-layout-set]");
  if (!btn) return;
  // "list" — канон уголос: порожнє значення сервер не відрізняє від
  // «поля не було», і покажчик вмикався б назавжди.
  const layout = btn.dataset.layoutSet === "nav" ? "nav" : "list";
  const body = new URLSearchParams({ scope: "handout", layout: layout });
  fetch("/account/look", {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: body.toString(),
    credentials: "same-origin",
  })
    .then((response) => {
      if (response.ok) { window.location.reload(); return; }
      if (window.showToast) window.showToast("Не вдалось зберегти розкладку", "error");
    })
    .catch(() => {
      if (window.showToast) window.showToast("Не вдалось зберегти розкладку", "error");
    });
});

// ── Памʼять згортання карток (аудит 05.09.26, UX 1.6) ───────────────────
// Кожна галочка «знайдено» підмінює весь `#handout-list` через HTMX, тож
// клас `.is-collapsed`, поставлений рукою, зникав разом зі старим DOM: усе,
// що оператор згорнув за ранок, розгорталося назад після КОЖНОЇ галочки.
// Ключ — клієнт + день, а не позиція в списку: позиція зсувається при зміні
// фільтра дня, і згорнутою «прокидалась» би чужа картка.
const HANDOUT_COLLAPSE_PREFIX = "handout:collapsed:";

function handoutCollapseKey(card) {
  const client = card.dataset.client || "";
  const day = card.dataset.day || "all";
  if (!client) return null;
  return HANDOUT_COLLAPSE_PREFIX + client + ":" + day;
}

function saveHandoutCollapsed(card, collapsed) {
  const key = handoutCollapseKey(card);
  if (!key) return;
  // Записуємо тільки згорнуті. Розгорнута картка — стан за замовчуванням,
  // і зберігати його означало б засмічувати сховище на кожного клієнта.
  if (collapsed) KMStore.set(key, "1");
  else KMStore.remove(key);
}

function restoreHandoutCollapsed() {
  document.querySelectorAll(".ccard[data-client]").forEach((card) => {
    const key = handoutCollapseKey(card);
    if (!key) return;
    const saved = KMStore.get(key);
    // Нічого не збережено — лишаємо серверний стан (виданий клієнт приходить
    // згорнутим сам).
    if (saved === null) return;
    const collapsed = saved === "1";
    card.classList.toggle("is-collapsed", collapsed);
    const toggle = card.querySelector(".card-collapse");
    if (toggle) toggle.setAttribute("aria-expanded", String(!collapsed));
  });
}

document.addEventListener("click", (event) => {
  const toggle = event.target.closest(".card-collapse");
  if (!toggle) return;
  const card = toggle.closest(".ccard");
  // Сам клас перемикає загальний обробник у app.js; тут лише запамʼятовуємо
  // результат. Обидва слухають ту саму подію, тож стан уже актуальний.
  if (card) saveHandoutCollapsed(card, card.classList.contains("is-collapsed"));
});

// ── Пошук клієнта в списку (UX 1.6) ─────────────────────────────────────
// Чисто клієнтський фільтр: ховає картки, що не збіглись, і НЕ чіпає порядок
// решти — правило №1 видачі (порядок фіксується на початку дня, жодного
// авто-пересортування під руками).
function applyHandoutFilter() {
  const input = document.getElementById("handout-find");
  if (!input) return;
  const needle = input.value.trim().toLowerCase();
  const counter = document.getElementById("handout-find-count");
  let shown = 0;
  let total = 0;
  document.querySelectorAll(".ccard[data-client]").forEach((card) => {
    total += 1;
    const name = (card.dataset.client || "").toLowerCase();
    const match = !needle || name.includes(needle);
    card.hidden = !match;
    if (match) shown += 1;
    // Покажчик дня ліворуч мусить ховати ті самі рядки, інакше клік по ньому
    // веде до схованої картки.
    const nav = document.querySelector('.daynav a[href="#' + card.id + '"]');
    if (nav) nav.hidden = !match;
  });
  if (counter) {
    counter.hidden = !needle;
    counter.textContent = needle ? shown + " з " + total : "";
  }
}

document.addEventListener("input", (event) => {
  if (event.target && event.target.id === "handout-find") applyHandoutFilter();
});

document.addEventListener("DOMContentLoaded", () => {
  restoreHandoutCollapsed();
  applyHandoutFilter();
});

// Відновлюємо ПІСЛЯ settle, не після swap. Картка клієнта має id
// (`handout-client-N`), а HTMX на фазі settle звіряє старий і новий вузли за
// id і повертає їм атрибути зі свого списку — серед яких `class`. Клас
// `.is-collapsed`, поставлений на `htmx:afterSwap`, через 20 мс мовчки
// затирався серверним значенням, і згортання «злітало» рівно так само, як до
// правки. Фільтр цього не помітив, бо ховає через властивість `hidden`, якої
// в тому списку немає — саме тому симптом виглядав вибірковим.
document.body.addEventListener("htmx:afterSettle", (event) => {
  if (!event.target || event.target.id !== "handout-list") return;
  restoreHandoutCollapsed();
  applyHandoutFilter();
  applyHandoutLooking();
});

// ── «Кого я шукаю»: підсвітка роботи, чиї файли відкрито в прев'ю ────────
// Прохання власника 10.09.26: відкрив теку в прев'ю, звірив форму, пішов до
// лотка — і вже не памʼятаєш, якого клієнта шукав. Тож рядок роботи і її
// клієнт (картка, рядок плаского списку, пункт покажчика дня) лишаються
// підсвіченими і ПІСЛЯ закриття прев'ю — доки не відкриєш інші файли або не
// поставиш цій роботі галочку «знайдено».
//
// Ключ — id роботи (`data-order` на `.wrow`), а не DOM-вузол: список свапають
// і галочка, і пульс, тож старий вузол зникає, і підсвітку ставимо заново
// після settle (з тієї ж причини, що й згортання карток вище). У памʼяті
// сторінки, без сховища: після перезавантаження шукати вже нікого.
let handoutLookingOrder = null;

function applyHandoutLooking() {
  document.querySelectorAll(".is-looking").forEach((el) => el.classList.remove("is-looking"));
  if (!handoutLookingOrder) return;
  const row = document.querySelector(
    '#handout-list .wrow[data-order="' + handoutLookingOrder + '"]'
  );
  // Знайдено — пошук закінчено; зникла зі списку — підсвічувати нічого.
  if (!row || row.classList.contains("found")) {
    handoutLookingOrder = null;
    return;
  }
  const flat = row.closest(".flatrow");
  if (flat) {
    // У пласкому списку рядок роботи і є рядком клієнта — підсвічуємо його
    // цілком, разом з іменем.
    flat.classList.add("is-looking");
    return;
  }
  row.classList.add("is-looking");
  const card = row.closest(".ccard");
  if (!card) return;
  card.classList.add("is-looking");
  const nav = document.querySelector('.daynav a[href="#' + card.id + '"]');
  if (nav) nav.classList.add("is-looking");
}

// Та сама подія, що відкриває прев'ю (stl-preview.js). Прев'ю не чіпаємо:
// воно своє, а ми лише запамʼятовуємо, звідки його відкрили.
document.addEventListener("click", (event) => {
  const trigger = event.target.closest("#handout-list [data-stl-preview-token]");
  if (!trigger) return;
  const row = trigger.closest(".wrow[data-order]");
  if (!row) return;
  handoutLookingOrder = row.dataset.order;
  applyHandoutLooking();
});
// ── QC-чеклист перед «знайдено» (опційний, вимкнений за замовчуванням) ──
//
// Гейт свідомо клієнтський: три галочки — це зупинка ПОГЛЯДУ, а не дані
// (app/services/handout_qc.py). Сервер лишається тим самим `mark-found`.
//
// Слухач стоїть у фазі ПЕРЕХОПЛЕННЯ і глушить подію: htmx слухає `submit` на
// тілі документа, тож без `stopPropagation` запит пішов би паралельно з
// діалогом — і галочка ставилась би до звірки, тобто гейта не було б узагалі.
let qcForm = null;
// Елемент, з якого відкрили діалог (зазвичай кнопка «Знайдено» рядка) —
// фокус повертається на нього (чи на кнопку submit форми) при закритті,
// інакше фокус лишався б на видаленому діалозі й «падав» на body.
let qcTriggerEl = null;

function qcDialog() {
  return document.getElementById("qc-dialog");
}

function qcItems() {
  const dialog = qcDialog();
  return dialog ? Array.from(dialog.querySelectorAll("[data-qc-item]")) : [];
}

function qcReset() {
  qcItems().forEach((box) => { box.checked = false; });
  const confirm = qcDialog() && qcDialog().querySelector("[data-qc-confirm]");
  if (confirm) confirm.disabled = true;
}

// Фокус-пастка модалки: без неї Tab виводив фокус за межі діалогу на
// приховані елементи списку позаду — role="dialog" aria-modal="true" сам
// по собі цього не забороняє, браузер таку поведінку не дає безкоштовно.
function qcFocusable() {
  const dialog = qcDialog();
  if (!dialog) return [];
  return Array.from(
    dialog.querySelectorAll('button:not([disabled]), input:not([disabled]), [href], [tabindex]:not([tabindex="-1"])')
  );
}

function qcClose() {
  const dialog = qcDialog();
  const form = qcForm;
  if (dialog) dialog.hidden = true;
  qcForm = null;
  qcReset();
  const submitBtn = form && form.querySelector('button[type="submit"], input[type="submit"]');
  const restoreTarget = submitBtn || qcTriggerEl;
  qcTriggerEl = null;
  if (restoreTarget && typeof restoreTarget.focus === "function") restoreTarget.focus();
}

function qcOpen(form) {
  const dialog = qcDialog();
  if (!dialog) return false;
  qcForm = form;
  qcTriggerEl = document.activeElement;
  qcReset();
  const label = dialog.querySelector("[data-qc-work]");
  if (label) label.textContent = form.dataset.qcLabel || "";
  dialog.hidden = false;
  const first = qcItems()[0];
  if (first) first.focus();
  return true;
}

document.addEventListener("submit", (event) => {
  const form = event.target;
  if (!form || form.dataset.qc !== "1") return;
  // Другий прохід: діалог уже пройдено, пускаємо форму до htmx.
  if (form.dataset.qcPassed === "1") {
    delete form.dataset.qcPassed;
    return;
  }
  // Діалога на сторінці немає (шаблон не вставлено) — не блокуємо роботу
  // операторові через нашу ж помилку: хай іде звичайний клік.
  if (!qcOpen(form)) return;
  event.preventDefault();
  event.stopPropagation();
}, true);

document.addEventListener("change", (event) => {
  if (!event.target || !event.target.matches("[data-qc-item]")) return;
  const confirm = qcDialog() && qcDialog().querySelector("[data-qc-confirm]");
  if (confirm) confirm.disabled = qcItems().some((box) => !box.checked);
});

document.addEventListener("click", (event) => {
  const dialog = qcDialog();
  if (!dialog || dialog.hidden) return;
  if (event.target.closest("[data-qc-cancel]") || event.target === dialog) {
    qcClose();
    return;
  }
  if (!event.target.closest("[data-qc-confirm]")) return;
  if (qcItems().some((box) => !box.checked)) return;
  const form = qcForm;
  qcClose();
  if (!form) return;
  form.dataset.qcPassed = "1";
  form.requestSubmit();
});

document.addEventListener("keydown", (event) => {
  const dialog = qcDialog();
  if (!dialog || dialog.hidden) return;
  if (event.key === "Escape") {
    qcClose();
    return;
  }
  if (event.key !== "Tab") return;
  const focusable = qcFocusable();
  if (!focusable.length) return;
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
});

// ── Перемикач порядку: картки по клієнтах ⇄ рядки в порядку таблиці ──────
// Той самий шлях, що й розкладка: вибір живе на акаунті, тому їде за
// оператором. Перезавантаження, а не свап: режим міняє структуру всього
// списку, і підмінити половину означало б показати екран наполовину одним
// виглядом, наполовину іншим.
document.addEventListener("click", (event) => {
  const btn = event.target.closest("[data-flow-set]");
  if (!btn) return;
  const body = new URLSearchParams({
    scope: "handout",
    // Розкладку шлемо ту, що вже стоїть: сервер приймає обидва поля разом,
    // і пропущене означало б «скинути в дефолт».
    layout: document.body.dataset.handoutLayout || "",
    flow: btn.dataset.flowSet || "",
  });
  fetch("/account/look", { method: "POST", body, credentials: "same-origin" })
    .then(() => window.location.reload())
    .catch(() => window.location.reload());
});

// ── Галочка «знайдено» відгукується ОДРАЗУ ──────────────────────────────
// Клік по галочці свапає весь `#handout-list`, і до 09.09.26 оператор бачив
// результат лише коли приходила відповідь — на бойових замірах 0.7-2.1 с.
// Найдорожче в тій відповіді (нечітке зіставлення клієнтів із теками) тепер
// кешується, але навіть 0.2 с на екрані, де галочки клацають підряд, читаються
// як гальмо.
//
// Тому малюємо стан ДО відповіді. Це не обман: сервер приймає клік завжди
// (робота існує, статус змінюється), а якщо запит таки впав — свапу не буде,
// і ми повертаємо рядок як був. Мовчазно «залипла» галочка тут гірша за
// секунду очікування: оператор пішов би до наступної коронки з думкою, що ця
// вже позначена.
function handoutRowForm(detail) {
  const form = detail && detail.elt;
  if (!form || !form.matches || !form.matches("form")) return null;
  const path = (detail.requestConfig && detail.requestConfig.path) || "";
  if (!/\/orders\/\d+\/(un)?mark-found$/.test(path)) return null;
  const row = form.closest(".wrow");
  return row ? { row, marking: path.endsWith("/mark-found") } : null;
}

document.body.addEventListener("htmx:beforeRequest", (event) => {
  const hit = handoutRowForm(event.detail);
  if (!hit) return;
  // Запамʼятовуємо, що було, — щоб було чим відкотитись на помилці.
  hit.row.dataset.foundWas = hit.row.classList.contains("found") ? "1" : "0";
  hit.row.classList.toggle("found", hit.marking);
});

["htmx:responseError", "htmx:sendError", "htmx:timeout"].forEach((name) => {
  document.body.addEventListener(name, (event) => {
    const hit = handoutRowForm(event.detail);
    if (!hit) return;
    hit.row.classList.toggle("found", hit.row.dataset.foundWas === "1");
    delete hit.row.dataset.foundWas;
  });
});
