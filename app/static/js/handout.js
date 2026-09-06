// Видача (CLAUDE.md §9.4).
//
// Тут лише перемикач «Плитки / Рядки» і його відновлення. Головне: відмітка
// «знайдено» підмінює список карток через HTMX, а клас режиму живе на самому
// списку — тому після кожної підміни режим треба ставити наново, інакше
// галочка мовчки скидала «Плитки» на «Рядки».

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
  // Селектор звужено до СВОГО перемикача. Поруч стоїть другий (розкладка
  // екрана) з тим самим класом .view-btn, і широкий запит знімав з нього
  // підсвітку на кожному відновленні — кнопка «Список + покажчик» виглядала
  // невибраною, хоча розкладка була саме та.
  document.querySelectorAll(".view-toggle .view-btn").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.viewMode === (tiles ? "tiles" : "rows"));
  });
}

document.addEventListener("click", (event) => {
  const btn = event.target.closest(".view-btn");
  if (!btn) return;
  // Кнопки РОЗКЛАДКИ мають той самий клас .view-btn, але не мають
  // data-view-mode — і без цього рядка падали сюди ж, писали в localStorage
  // "rows" і мовчки викидали оператора з «Плиток». А плитки на видачі — це
  // STL-прев'ю, тобто головний інструмент звірки (§9.4): втратити його від
  // кліку по сусідньому перемикачу означає зламати сам сенс екрана.
  // Підсвітку від цієї ж колізії вже лікували в applyHandoutView, але лише в
  // один бік — ось другий.
  if (btn.hasAttribute("data-layout-set")) return;
  const mode = btn.dataset.viewMode === "tiles" ? "tiles" : "rows";
  // Через KMStore: префікс kuubmill:v1: і ковтання приватного режиму — там.
  KMStore.set(HANDOUT_VIEW_KEY, mode);
  applyHandoutView(mode);
});

function restoreHandoutView() {
  if (!document.querySelector("[data-view-root]")) return;
  const saved = KMStore.get(HANDOUT_VIEW_KEY) || "rows";
  applyHandoutView(saved);
}

document.addEventListener("DOMContentLoaded", restoreHandoutView);

// Відмітка «знайдено» підмінює список карток через HTMX, а `as-tiles` живе на
// самому списку — без цього кожна галочка мовчки скидала «Плитки» на «Рядки».
document.body.addEventListener("htmx:afterSwap", (event) => {
  if (event.target && event.target.id === "handout-list") restoreHandoutView();
});

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
  const layout = btn.dataset.layoutSet === "nav" ? "nav" : "";
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
});
