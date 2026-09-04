// Режим редагування віджетів черги: перетягування смуги верстатів угорі та
// секцій правої панелі. Вмикається кнопкою в шестерні вигляду.
//
// Чому порядок застосовує СЕРВЕР, а JS лише зберігає: обидві зони живуть під
// поллами (смуга свапається кожні 10 с, секції печей/верстатів — кожні 30 с,
// черга — 15 с). Порядок, виставлений лише клієнтом, помирав би разом зі
// старою розміткою — той самий урок, що з класами згортання на body. Тому
// сервер малює `style="order:N"` на секціях і сортує картки верстатів, а
// клієнт після перетягування шле новий порядок і більше ні за чим не стежить.
//
// draggable вмикається ЛИШЕ в режимі: чіп верстата — це <a href>, а посилання
// перетягуються нативно, і без цього оператор випадково тягав би їх у сусідню
// вкладку. З тієї ж причини в режимі гаситься клік.
(function () {
  "use strict";

  var BODY_CLASS = "widget-edit";
  var LS_KEY = "widgetEditMode";

  function lsSet(value) {
    try { localStorage.setItem(LS_KEY, value ? "1" : "0"); } catch (e) {}
  }

  function isOn() {
    return document.body.classList.contains(BODY_CLASS);
  }

  // ── Що саме тягнемо ───────────────────────────────────────────────────────
  // Дві зони, однакова механіка, різні ключі: секції правої панелі впізнаються
  // за `data-sec`, чіпи верстатів — за `data-mid` (id рядка machines).
  var ZONES = [
    { container: ".q2 .side-panel", item: ".side-sec", key: "sec", scope: "side" },
    { container: ".q2 .mstrip", item: "[data-mid]", key: "mid", scope: "strip" }
  ];

  function zoneOf(element) {
    for (var i = 0; i < ZONES.length; i += 1) {
      var item = element.closest(ZONES[i].item);
      if (item && item.closest(ZONES[i].container)) return { zone: ZONES[i], item: item };
    }
    return null;
  }

  function itemsOf(zone) {
    var container = document.querySelector(zone.container);
    if (!container) return [];
    return Array.prototype.filter.call(
      container.querySelectorAll(zone.item),
      function (el) { return el.dataset[zone.key]; }
    );
  }

  // Порядок читаємо з CSS-властивості `order` (її ставить сервер), а не з
  // порядку в DOM: після перетягування ми міняємо саме її, і DOM лишається
  // тим, який прийшов з полла.
  function currentOrder(zone) {
    return itemsOf(zone)
      .slice()
      .sort(function (a, b) {
        return (parseInt(a.style.order, 10) || 0) - (parseInt(b.style.order, 10) || 0);
      });
  }

  function applyOrder(zone, ordered) {
    ordered.forEach(function (el, index) { el.style.order = String(index); });
  }

  function save(zone, ordered) {
    var body = new URLSearchParams({
      scope: zone.scope,
      order: ordered.map(function (el) { return el.dataset[zone.key]; }).join(",")
    });
    fetch("/account/layout", { method: "POST", body: body, credentials: "same-origin" })
      .then(function (response) {
        if (!response.ok && window.showToast) {
          window.showToast("Не вдалося зберегти порядок віджетів", "error");
        }
      })
      .catch(function () {
        if (window.showToast) window.showToast("Не вдалося зберегти порядок віджетів", "error");
      });
  }

  // ── Перетягування ─────────────────────────────────────────────────────────
  var dragging = null;
  var draggingZone = null;

  function markDraggable() {
    var on = isOn();
    ZONES.forEach(function (zone) {
      itemsOf(zone).forEach(function (el) {
        if (on) el.setAttribute("draggable", "true");
        else el.removeAttribute("draggable");
      });
    });
  }

  document.addEventListener("dragstart", function (event) {
    if (!isOn()) return;
    var found = zoneOf(event.target);
    if (!found) return;
    dragging = found.item;
    draggingZone = found.zone;
    dragging.classList.add("is-dragging");
    if (event.dataTransfer) {
      event.dataTransfer.effectAllowed = "move";
      // Firefox не починає перетягування без даних.
      try { event.dataTransfer.setData("text/plain", dragging.dataset[found.zone.key]); } catch (e) {}
    }
  });

  document.addEventListener("dragover", function (event) {
    if (!dragging) return;
    var found = zoneOf(event.target);
    if (!found || found.zone !== draggingZone || found.item === dragging) return;
    event.preventDefault();
    if (event.dataTransfer) event.dataTransfer.dropEffect = "move";

    var ordered = currentOrder(draggingZone);
    var from = ordered.indexOf(dragging);
    var to = ordered.indexOf(found.item);
    if (from < 0 || to < 0) return;
    ordered.splice(from, 1);
    ordered.splice(to, 0, dragging);
    applyOrder(draggingZone, ordered);
  });

  document.addEventListener("drop", function (event) {
    if (!dragging) return;
    event.preventDefault();
  });

  document.addEventListener("dragend", function () {
    if (!dragging) return;
    dragging.classList.remove("is-dragging");
    var zone = draggingZone;
    dragging = null;
    draggingZone = null;
    if (zone) save(zone, currentOrder(zone));
  });

  // Клік у режимі не має відкривати верстат/пічку — палець уже на елементі,
  // який зараз тягнуть.
  document.addEventListener("click", function (event) {
    if (!isOn()) return;
    var found = zoneOf(event.target);
    if (found && event.target.closest("a")) {
      event.preventDefault();
      event.stopPropagation();
    }
  }, true);

  // Клавіатура: той самий рух без миші. Ctrl+стрілки на елементі в фокусі.
  document.addEventListener("keydown", function (event) {
    if (!isOn() || !event.ctrlKey) return;
    var step = event.key === "ArrowUp" || event.key === "ArrowLeft" ? -1
             : event.key === "ArrowDown" || event.key === "ArrowRight" ? 1 : 0;
    if (!step) return;
    var found = zoneOf(event.target);
    if (!found) return;
    var ordered = currentOrder(found.zone);
    var from = ordered.indexOf(found.item);
    var to = from + step;
    if (from < 0 || to < 0 || to >= ordered.length) return;
    event.preventDefault();
    ordered.splice(from, 1);
    ordered.splice(to, 0, found.item);
    applyOrder(found.zone, ordered);
    save(found.zone, ordered);
  });

  // ── Вмикання ──────────────────────────────────────────────────────────────
  function setMode(on) {
    document.body.classList.toggle(BODY_CLASS, on);
    lsSet(on);
    document.querySelectorAll("[data-widget-edit-toggle]").forEach(function (btn) {
      btn.setAttribute("aria-pressed", String(on));
    });
    markDraggable();
  }

  document.addEventListener("click", function (event) {
    var btn = event.target.closest("[data-widget-edit-toggle]");
    if (!btn) return;
    setMode(!isOn());
  });

  // Полли підміняють смугу й секції цілком — атрибут draggable треба ставити
  // наново, інакше режим «згасає» під рукою оператора через 10 секунд.
  document.body.addEventListener("htmx:afterSwap", markDraggable);
  document.querySelectorAll("[data-widget-edit-toggle]").forEach(function (btn) {
    btn.setAttribute("aria-pressed", String(isOn()));
  });
  markDraggable();
})();
