/*
 * Клієнтський бік вимірювання затримок (пара до app/perf.py).
 *
 * Навіщо. Сервер бачить свій час і замовкає. Але оператор чекає ще й на те,
 * що йде ПІСЛЯ відповіді: передача, свап HTMX, перемальовка таблиці. На черзі
 * в кілька сотень рядків це та сама величина, що й увесь сервер, — і в логах
 * її не було ніколи. Тому кожен запит зіставляється з серверною пробою за
 * заголовком X-Perf-Id, і сюди дописується решта шляху.
 *
 * Що міряємо для HTMX-взаємодії:
 *   request  — htmx:beforeRequest → htmx:beforeSwap (мережа + сервер)
 *   swap     — htmx:beforeSwap → htmx:afterSettle (заміна розмітки)
 *   paint    — afterSettle → наступний кадр (реальна перемальовка)
 *   total    — від кліка до намальованого
 *
 * Для звичайної навігації числа беруться з Navigation Timing.
 *
 * Дешевизна тут — вимога, а не побажання: цей код крутиться на екрані, який
 * відкритий цілу зміну. Жодних спостерігачів, жодних таймерів; лише події,
 * які HTMX і так шле, плюс один requestAnimationFrame на взаємодію.
 */
(function () {
  "use strict";

  const ENDPOINT = "/diag/perf/client";
  const pending = [];
  let flushTimer = null;

  function queue(id, metrics) {
    if (!id) return;
    pending.push({ id: id, metrics: metrics });
    if (flushTimer) return;
    // Пачкою і з затримкою: інакше кожен клік давав би ДРУГИЙ запит одразу
    // за першим, і ми міряли б систему, яку самі ж і навантажили.
    flushTimer = window.setTimeout(flush, 4000);
  }

  function flush() {
    flushTimer = null;
    if (!pending.length) return;
    const batch = pending.splice(0, pending.length);
    const body = JSON.stringify(batch);
    try {
      // sendBeacon переживає закриття вкладки — інакше найцікавіші проби
      // (ті, після яких оператор пішов зі сторінки) губились би.
      if (navigator.sendBeacon) {
        navigator.sendBeacon(ENDPOINT, new Blob([body], { type: "application/json" }));
        return;
      }
    } catch (e) {
      /* нижче є запасний шлях */
    }
    fetch(ENDPOINT, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: body,
      keepalive: true,
    }).catch(() => {});
  }

  window.addEventListener("pagehide", flush);
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "hidden") flush();
  });

  // ── HTMX-взаємодії ────────────────────────────────────────────────────────
  // Стан тримаємо на самому XHR: одночасних запитів буває кілька (полл черги,
  // смуга печей, дія оператора), і спільна змінна їх переплутала б.
  document.body.addEventListener("htmx:beforeRequest", function (e) {
    const xhr = e.detail && e.detail.xhr;
    if (!xhr) return;
    xhr.__perfStart = performance.now();
  });

  document.body.addEventListener("htmx:beforeSwap", function (e) {
    const xhr = e.detail && e.detail.xhr;
    if (!xhr || !xhr.__perfStart) return;
    xhr.__perfResponded = performance.now();
    try {
      xhr.__perfId = xhr.getResponseHeader("X-Perf-Id") || "";
    } catch (err) {
      xhr.__perfId = "";
    }
  });

  document.body.addEventListener("htmx:afterSettle", function (e) {
    const xhr = e.detail && e.detail.xhr;
    if (!xhr || !xhr.__perfStart || !xhr.__perfId) return;
    const settled = performance.now();
    // Наступний кадр = момент, коли браузер справді намалював новий вміст.
    // Без цього «свап» виглядав би миттєвим навіть на важкій таблиці.
    window.requestAnimationFrame(function () {
      const painted = performance.now();
      queue(xhr.__perfId, {
        request: (xhr.__perfResponded - xhr.__perfStart) / 1000,
        swap: (settled - xhr.__perfResponded) / 1000,
        paint: (painted - settled) / 1000,
        total: (painted - xhr.__perfStart) / 1000,
      });
    });
  });

  // ── Звичайна навігація (клік по пункту рейки, перезавантаження) ───────────
  window.addEventListener("load", function () {
    window.setTimeout(function () {
      let nav = null;
      try {
        nav = performance.getEntriesByType("navigation")[0];
      } catch (e) {
        return;
      }
      if (!nav) return;
      const id = document.body.dataset.perfId || "";
      if (!id) return;
      queue(id, {
        request: Math.max(0, (nav.responseStart - nav.requestStart) / 1000),
        transfer: Math.max(0, (nav.responseEnd - nav.responseStart) / 1000),
        // Розбір HTML + застосування стилів + перший малюнок сторінки.
        render: Math.max(0, (nav.domContentLoadedEventEnd - nav.responseEnd) / 1000),
        total: Math.max(0, (nav.loadEventEnd || performance.now()) / 1000 - nav.startTime / 1000),
      });
    }, 0);
  });
})();
