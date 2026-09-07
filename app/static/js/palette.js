// Глобальна палітра команд: Ctrl+K / Cmd+K (аудит 05.09.26, крок 3.3).
//
// ЧОМУ ВОНА ПОТРІБНА. Екранів уже шістнадцять, рейка вміє згортатись до самих
// іконок, а на сторінках налаштувань взагалі підмінюється власним підменю —
// тобто «дійти до потрібного екрана» коштує двох-трьох кліків і памʼяті, де
// саме живе пункт. Палітра дає один вхід із клавіатури, однаковий на будь-якому
// екрані, і не забирає жодного пікселя в черги.
//
// ЩО САМЕ ЗВІДКИ БЕРЕТЬСЯ. Перелік екранів дає сервер (/palette/commands): що
// людині взагалі можна бачити, вирішує роль, і показати оператору «Журнал
// синку», щоб після Enter віддати 403, гірше, ніж не показати зовсім. Роботи
// шукає /palette/search — вузький SQL із LIMIT; повноцінний екран /search
// стоїть останнім рядком як «показати все», тому логіка пошуку не роздвоюється.
//
// ЧОМУ КЛАСИЧНИЙ СКРИПТ, А НЕ МОДУЛЬ. Так вантажиться весь фронтенд проєкту
// (base.html, defer, спільний глобальний простір) — див. tests/test_frontend_assets.py.
(function () {
  "use strict";

  var COMMANDS_URL = "/palette/commands";
  var SEARCH_URL = "/palette/search?q=";
  var SEARCH_MIN = 2;
  // Пауза перед запитом. Достатньо, щоб «24122» пішло одним запитом, а не
  // п'ятьма, і замало, щоб чекання відчувалось.
  var SEARCH_DEBOUNCE_MS = 220;

  // ── спільна вартова: людина зараз НАБИРАЄ? ────────────────────────────
  // Живе тут, бо потрібна кожній однолітерній гарячій клавіші (J/K у тріажі —
  // mail.js). Найчастіша вада таких фіч рівно одна: «j» у коментарі перемикає
  // рядок замість того, щоб надрукуватись. Одну умову дешевше тримати чесною,
  // ніж три її копії, тому вона тут одна на весь застосунок.
  function isTyping(el) {
    if (!el) return false;
    if (el.isContentEditable) return true;
    var tag = (el.tagName || "").toUpperCase();
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return true;
    // Кастомні поля виглядають як <div>, але в них НАБИРАЮТЬ — літера мусить
    // долетіти як літера.
    var role = el.getAttribute ? el.getAttribute("role") : null;
    if (role === "textbox" || role === "combobox" || role === "searchbox") return true;
    // Курсор стоїть у contenteditable, а подія прийшла від вкладеного вузла.
    if (el.closest && el.closest('[contenteditable=""], [contenteditable="true"]')) return true;
    return false;
  }

  window.KMKeys = { isTyping: isTyping };

  var root = null;
  var input = null;
  var list = null;
  var rows = [];
  var sel = 0;
  var commands = null; // кеш на сесію сторінки: перелік екранів не змінюється
  var lastFocus = null;
  var searchTimer = null;
  var searchToken = 0; // порядковий номер запиту — пізня відповідь не перебиває свіжу

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function build() {
    if (root) return;
    root = document.createElement("div");
    root.className = "kmpal";
    root.setAttribute("role", "dialog");
    root.setAttribute("aria-modal", "true");
    root.setAttribute("aria-label", "Палітра команд");
    root.innerHTML =
      '<div class="kmpal-box">' +
      '  <div class="kmpal-top">' +
      '    <svg class="kmpal-ico" viewBox="0 0 24 24" aria-hidden="true">' +
      '      <circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>' +
      '    <input type="text" class="kmpal-input" autocomplete="off" spellcheck="false"' +
      '           role="combobox" aria-expanded="true" aria-autocomplete="list"' +
      '           aria-controls="kmpal-list"' +
      '           placeholder="Куди перейти або що знайти? напр. «видача», «24122»">' +
      "  </div>" +
      '  <ul class="kmpal-list" id="kmpal-list" role="listbox" aria-label="Результати"></ul>' +
      '  <div class="kmpal-foot"><span>↑ ↓ вибір</span><span>Enter відкрити</span>' +
      "<span>Esc закрити</span></div>" +
      "</div>";
    document.body.appendChild(root);
    input = root.querySelector(".kmpal-input");
    list = root.querySelector(".kmpal-list");

    input.addEventListener("input", function () { schedule(input.value); });
    input.addEventListener("keydown", onInputKey);
    // Клік повз коробку закриває: оверлей накриває весь екран, і без цього
    // єдиний вихід — клавіатура.
    root.addEventListener("click", function (event) { if (event.target === root) close(); });
    list.addEventListener("click", function (event) {
      var btn = event.target.closest ? event.target.closest("button[data-i]") : null;
      if (btn) pick(parseInt(btn.getAttribute("data-i"), 10));
    });
  }

  function render() {
    if (!rows.length) {
      list.innerHTML = '<li class="kmpal-empty">Нічого не знайдено</li>';
      return;
    }
    var html = "";
    for (var i = 0; i < rows.length; i++) {
      var r = rows[i];
      html +=
        '<li class="kmpal-item" role="option" id="kmpal-opt-' + i + '"' +
        (i === sel ? ' aria-selected="true"' : "") + ">" +
        '<button type="button" data-i="' + i + '">' +
        '<span class="kmpal-lb">' + esc(r.label) + "</span>" +
        (r.sub ? '<span class="kmpal-sub">' + esc(r.sub) + "</span>" : "") +
        (r.group ? '<span class="kmpal-g">' + esc(r.group) + "</span>" : "") +
        "</button></li>";
    }
    list.innerHTML = html;
    mark();
  }

  function mark() {
    var items = list.children;
    for (var i = 0; i < items.length; i++) {
      if (i === sel) {
        items[i].setAttribute("aria-selected", "true");
        if (items[i].scrollIntoView) items[i].scrollIntoView({ block: "nearest" });
      } else {
        items[i].removeAttribute("aria-selected");
      }
    }
    input.setAttribute("aria-activedescendant", rows.length ? "kmpal-opt-" + sel : "");
  }

  function move(step) {
    if (!rows.length) return;
    sel = (sel + step + rows.length) % rows.length; // по колу: список короткий
    mark();
  }

  function pick(i) {
    var r = rows[i];
    if (!r || !r.href) return;
    close();
    window.location.href = r.href;
  }

  function matches(item, q) {
    if (!q) return true;
    var hay = (item.label + " " + (item.group || "") + " " + (item.keywords || "")).toLowerCase();
    return hay.indexOf(q) !== -1;
  }

  // Список = екрани, що підійшли + знайдені роботи + завжди останній рядок
  // «показати все на /search». Останній рядок навмисно не зникає: він
  // страхує випадок «палітра нічого не знайшла, а робота є» (наприклад,
  // збіг лише в полі, якого немає в швидкому запиті).
  function compose(q, found) {
    var out = [];
    var i;
    var cmds = commands || [];
    for (i = 0; i < cmds.length; i++) {
      if (matches(cmds[i], q)) out.push({ label: cmds[i].label, group: cmds[i].group, href: cmds[i].href });
    }
    for (i = 0; i < found.length; i++) out.push(found[i]);
    if (q) {
      out.push({
        label: "Шукати «" + q + "» серед усіх робіт",
        group: "Пошук",
        href: "/search?q=" + encodeURIComponent(q),
      });
    }
    return out;
  }

  var lastQuery = "";
  var lastFound = [];

  function schedule(raw) {
    var q = (raw || "").trim().toLowerCase();
    lastQuery = q;
    // Екрани фільтруються миттєво, локально: чекати на мережу, щоб показати
    // «Видача», було б помітним гальмом на дії, яка має бути миттєвою.
    lastFound = q ? lastFound : [];
    rows = compose(q, q ? lastFound : []);
    sel = 0;
    render();

    if (searchTimer) window.clearTimeout(searchTimer);
    if (q.length < SEARCH_MIN) { lastFound = []; return; }
    var token = ++searchToken;
    searchTimer = window.setTimeout(function () {
      fetch(SEARCH_URL + encodeURIComponent(q), { credentials: "same-origin" })
        .then(function (r) { return r.ok ? r.json() : { items: [] }; })
        .then(function (data) {
          // Відповідь на застарілий запит приходить після свіжішої — тоді її
          // місце вже зайняте, і підставляти її означало б показати результат
          // не того, що людина зараз бачить у полі.
          if (token !== searchToken || !isOpen()) return;
          lastFound = data.items || [];
          rows = compose(lastQuery, lastFound);
          if (sel >= rows.length) sel = 0;
          render();
        })
        .catch(function () { /* мережа впала — лишаються екрани й /search */ });
    }, SEARCH_DEBOUNCE_MS);
  }

  function loadCommands() {
    if (commands) return Promise.resolve(commands);
    return fetch(COMMANDS_URL, { credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.json() : { items: [] }; })
      .then(function (data) { commands = data.items || []; return commands; })
      .catch(function () { commands = []; return commands; });
  }

  function isOpen() {
    return !!root && root.classList.contains("is-open");
  }

  function open() {
    build();
    if (isOpen()) return;
    lastFocus = document.activeElement;
    root.classList.add("is-open");
    input.value = "";
    lastQuery = "";
    lastFound = [];
    rows = compose("", []);
    sel = 0;
    render();
    input.focus();
    loadCommands().then(function () {
      if (!isOpen()) return;
      rows = compose(lastQuery, lastFound);
      render();
    });
  }

  function close() {
    if (!isOpen()) return;
    root.classList.remove("is-open");
    if (searchTimer) { window.clearTimeout(searchTimer); searchTimer = null; }
    // Фокус повертається туди, звідки палітру відкрили: інакше після Esc
    // клавіатура опиняється на <body> і наступний Tab починає з початку сторінки.
    if (lastFocus && lastFocus.focus) { try { lastFocus.focus(); } catch (e) { /* вузла вже немає */ } }
    lastFocus = null;
  }

  function onInputKey(event) {
    if (event.key === "ArrowDown") { event.preventDefault(); move(1); }
    else if (event.key === "ArrowUp") { event.preventDefault(); move(-1); }
    else if (event.key === "Enter") { event.preventDefault(); pick(sel); }
    else if (event.key === "Escape") { event.preventDefault(); close(); }
    else if (event.key === "Tab") { event.preventDefault(); } // фокус лишається в полі
  }

  // Палітра налаштувань (settings_console.js) тримає той самий Ctrl+K і шукає
  // РОЗДІЛИ налаштувань — на своєму екрані вона корисніша за глобальну.
  // Тому там глобальна поступається, а не відкривається другою поверх.
  function settingsPaletteOwnsHotkey() {
    return !!document.querySelector("[data-scon-palette]");
  }

  document.addEventListener("keydown", function (event) {
    if ((event.ctrlKey || event.metaKey) && !event.altKey &&
        (event.key === "k" || event.key === "K" || event.code === "KeyK")) {
      if (settingsPaletteOwnsHotkey()) return;
      event.preventDefault();
      if (isOpen()) close(); else open();
      return;
    }
    if (event.key === "Escape" && isOpen()) close();
  });

  // Гачок для миші: будь-який елемент із data-km-palette відкриває те саме.
  //
  // Модифікатори й не-ліву кнопку пропускаємо. Гачок висить на СПРАВЖНЬОМУ
  // посиланні («Пошук» у рейці веде на /search і без JS), а Ctrl+клік по
  // посиланню означає «відкрий у новій вкладці» — забирати це в оператора
  // заради власної модалки не можна.
  document.addEventListener("click", function (event) {
    if (event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    var btn = event.target.closest ? event.target.closest("[data-km-palette]") : null;
    if (btn) { event.preventDefault(); open(); }
  });

  // Поле пошуку в шапці — той самий вхід. Фокус (миша АБО Tab) відкриває
  // палітру й одразу віддає їй курсор: інакше оператор набирав би текст у
  // полі, яке нічого не підказує, і мусив би тиснути Enter, щоб бодай щось
  // побачити. Саме поле лишається робочою формою на /search — без JS воно
  // працює як раніше.
  document.addEventListener("focusin", function (event) {
    var field = event.target;
    if (!field || !field.matches || !field.matches("input[data-km-palette]")) return;
    if (isOpen()) return;
    field.blur();
    open();
  });

  window.KMPalette = { open: open, close: close, isOpen: isOpen };
})();
