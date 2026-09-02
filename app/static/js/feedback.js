/* Форма зворотного зв'язку: маячок → бульбашка → панель.
   Розмітка — _feedback.html, стилі — feedback.css. Сабміт іде через HTMX
   (hx-post на формі); тут — відкриття/згортання, перемикачі, збір скріншотів
   (Ctrl+V / drag'n'drop / файл) і показ стану «Надіслано». */
(function () {
  "use strict";

  function init() {
    var root = document.getElementById("fb-root");
    if (!root) return; // немає сесії — віджета немає

    var beacon = document.getElementById("fb-beacon");
    var float = document.getElementById("fb-float");
    var form = document.getElementById("fb-form");
    var done = document.getElementById("fb-done");
    var closeBtn = document.getElementById("fb-close");
    var expandBtn = document.getElementById("fb-expand");
    var titleEl = document.getElementById("fb-title");
    var subEl = document.getElementById("fb-sub");
    var fileInput = document.getElementById("fb-file");
    var drop = document.getElementById("fb-drop");
    var shots = document.getElementById("fb-shots");
    var textEl = document.getElementById("fb-text");

    // Зібрані скріншоти тримаємо у власному DataTransfer і віддзеркалюємо в
    // input.files — так Ctrl+V, drag'n'drop і вибір файлу лягають в одне місце,
    // яке HTMX відправить у multipart.
    var bucket = new DataTransfer();

    // ── екран, з якого пишуть ──────────────────────────────────────────
    var SCREENS = {
      "/": "Черга", "/mail": "Нові з пошти", "/handout": "Ранкова видача",
      "/shift": "Зміна", "/furnaces": "Пічки", "/machines": "Верстати",
      "/clients": "Клієнти", "/archive": "Архів", "/stats": "Статистика",
      "/settings": "Налаштування", "/search": "Пошук", "/account": "Кабінет",
      "/feedback/inbox": "Вхідні"
    };
    function screenLabel() {
      var p = location.pathname;
      if (SCREENS[p]) return SCREENS[p];
      if (p.indexOf("/orders/") === 0) return "Картка роботи";
      if (p.indexOf("/settings") === 0) return "Налаштування";
      return p;
    }
    var label = screenLabel();
    var screenInput = document.getElementById("fb-screen");
    if (screenInput) screenInput.value = location.pathname;
    var screenLabelEl = document.getElementById("fb-screen-label");
    if (screenLabelEl) screenLabelEl.textContent = label;

    // ── відкриття / згортання ─────────────────────────────────────────
    function open() {
      float.hidden = false;
      root.setAttribute("data-open", "1");
      beacon.setAttribute("aria-expanded", "true");
      if (textEl) setTimeout(function () { textEl.focus(); }, 60);
    }
    function collapseToBubble() {
      float.classList.remove("fb-drawer");
      titleEl.textContent = "Швидка нотатка";
      subEl.textContent = "Помітив щось? Кинь сюди.";
    }
    function expand() {
      float.classList.add("fb-drawer");
      titleEl.textContent = "Повідомити детально";
      subEl.textContent = "Категорія, важливість, скріншоти.";
    }
    function reset() {
      form.reset();
      bucket = new DataTransfer();
      syncFiles();
      renderShots();
      setPressed(document.querySelector('[data-target="fb-kind"]'), "bug");
      setPressed(document.querySelector('[data-target="fb-severity"]'), "annoying");
      collapseToBubble();
      float.classList.remove("fb-sent");
      form.hidden = false;
      done.hidden = true;
    }
    function close() {
      float.hidden = true;
      root.setAttribute("data-open", "0");
      beacon.setAttribute("aria-expanded", "false");
      reset();
    }

    beacon.addEventListener("click", open);
    closeBtn.addEventListener("click", close);
    if (expandBtn) expandBtn.addEventListener("click", expand);
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && root.getAttribute("data-open") === "1") close();
    });

    // ── перемикачі (тип, важливість) ──────────────────────────────────
    function setPressed(group, val) {
      if (!group) return;
      var target = document.getElementById(group.getAttribute("data-target"));
      group.querySelectorAll("[data-val]").forEach(function (b) {
        b.setAttribute("aria-pressed", b.getAttribute("data-val") === val ? "true" : "false");
      });
      if (target) target.value = val;
    }
    root.querySelectorAll(".fb-seg, .fb-sev").forEach(function (group) {
      group.addEventListener("click", function (e) {
        var b = e.target.closest("[data-val]");
        if (b) setPressed(group, b.getAttribute("data-val"));
      });
    });

    // ── скріншоти ─────────────────────────────────────────────────────
    function syncFiles() { fileInput.files = bucket.files; }
    function renderShots() {
      shots.innerHTML = "";
      Array.prototype.forEach.call(bucket.files, function (file, i) {
        var cell = document.createElement("div");
        cell.className = "fb-shot";
        var img = document.createElement("img");
        img.alt = file.name || "скріншот";
        img.src = URL.createObjectURL(file);
        img.onload = function () { URL.revokeObjectURL(img.src); };
        cell.appendChild(img);
        var x = document.createElement("span");
        x.className = "fb-shot-x";
        x.textContent = "✕";
        x.title = "Прибрати";
        x.addEventListener("click", function () { removeAt(i); });
        cell.appendChild(x);
        shots.appendChild(cell);
      });
    }
    function addFiles(list) {
      Array.prototype.forEach.call(list, function (file) {
        if (file && file.type && file.type.indexOf("image/") === 0) {
          if (bucket.files.length < 4) bucket.items.add(file);
        }
      });
      syncFiles();
      renderShots();
    }
    function removeAt(idx) {
      var next = new DataTransfer();
      Array.prototype.forEach.call(bucket.files, function (f, i) {
        if (i !== idx) next.items.add(f);
      });
      bucket = next;
      syncFiles();
      renderShots();
    }

    fileInput.addEventListener("change", function () {
      // Браузер уже поклав вибране у fileInput.files — переносимо в bucket.
      addFiles(fileInput.files);
    });
    ["dragenter", "dragover"].forEach(function (ev) {
      drop.addEventListener(ev, function (e) { e.preventDefault(); drop.classList.add("fb-dragover"); });
    });
    ["dragleave", "drop"].forEach(function (ev) {
      drop.addEventListener(ev, function (e) { e.preventDefault(); drop.classList.remove("fb-dragover"); });
    });
    drop.addEventListener("drop", function (e) {
      if (e.dataTransfer && e.dataTransfer.files) addFiles(e.dataTransfer.files);
    });
    // Ctrl+V — лише коли віджет відкритий, щоб не хапати вставки в інших полях.
    document.addEventListener("paste", function (e) {
      if (root.getAttribute("data-open") !== "1") return;
      var items = e.clipboardData && e.clipboardData.items;
      if (!items) return;
      var files = [];
      for (var i = 0; i < items.length; i++) {
        if (items[i].kind === "file") {
          var f = items[i].getAsFile();
          if (f) files.push(f);
        }
      }
      if (files.length) { addFiles(files); e.preventDefault(); }
    });

    // ── результат сабміту ─────────────────────────────────────────────
    form.addEventListener("htmx:afterRequest", function (e) {
      var ok = e.detail && e.detail.successful &&
               e.detail.xhr && e.detail.xhr.status >= 200 && e.detail.xhr.status < 300;
      if (!ok) return; // помилку показав тост від toast_response, форма лишається
      float.classList.add("fb-sent");
      form.hidden = true;
      done.hidden = false;
      setTimeout(close, 2400);
    });
  }

  if (document.readyState !== "loading") init();
  else document.addEventListener("DOMContentLoaded", init);
})();
