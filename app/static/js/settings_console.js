/* Settings «Консоль» — client behaviour for /settings only.
 *
 * Loaded solely on the settings page (see settings.html). The rail sub-nav is
 * shared with materials/recognition/account, but those pages don't load this
 * file — their sub-nav links just navigate. Here we intercept the same-page
 * ones and switch sections without a reload.
 *
 *  - section switching (rail sub-nav + #hash + palette)
 *  - Ctrl+K command palette built from the rail sub-nav (respects role gates)
 *  - Стан системи self-check → POST /settings/selfcheck (real backend)
 */
(function () {
  "use strict";

  var main = document.querySelector("[data-scon]");
  if (!main) return;

  var sections = Array.prototype.slice.call(main.querySelectorAll(".scon-sec"));
  var railItems = Array.prototype.slice.call(document.querySelectorAll(".rail-sset"));

  function sectionKeys() {
    return sections.map(function (s) { return s.dataset.sec; });
  }

  // ── show one section ─────────────────────────────────────
  function show(key, focusSel) {
    var keys = sectionKeys();
    if (keys.indexOf(key) === -1) key = keys.indexOf("state") !== -1 ? "state" : keys[0];
    sections.forEach(function (s) {
      s.classList.toggle("is-shown", s.dataset.sec === key);
    });
    railItems.forEach(function (a) {
      var on = a.dataset.sec === key;
      a.classList.toggle("is-active", on);
      if (on) a.setAttribute("aria-current", "true");
      else a.removeAttribute("aria-current");
    });
    if (history.replaceState) history.replaceState(null, "", "#" + key);
    if (focusSel) {
      window.setTimeout(function () {
        var f = main.querySelector(focusSel);
        if (f) {
          f.focus({ preventScroll: false });
          f.classList.add("scon-flash");
          window.setTimeout(function () { f.classList.remove("scon-flash"); }, 1700);
        }
      }, 90);
    }
    main.scrollIntoView({ block: "start", behavior: "smooth" });
  }

  // ── rail sub-nav: intercept same-page section links ──────
  railItems.forEach(function (a) {
    a.addEventListener("click", function (e) {
      var key = a.dataset.sec;
      if (sectionKeys().indexOf(key) === -1) return; // external → let it navigate
      e.preventDefault();
      show(key);
    });
  });

  // ── command palette ──────────────────────────────────────
  var pal = buildPalette();
  document.body.appendChild(pal.root);

  function buildPalette() {
    // Entries come from the rendered rail sub-nav, so gates/labels stay in sync.
    var links = Array.prototype.slice.call(document.querySelectorAll(".rail-settings a[href]"));
    var entries = links
      .filter(function (a) { return !a.classList.contains("rail-settings-back"); })
      .map(function (a) {
        var label = (a.querySelector(".rail-label") || a).textContent.trim();
        var sec = a.dataset.sec || "";
        var group = "";
        var prev = a.closest(".rail-nav-group");
        var lbl = prev && prev.previousElementSibling;
        if (lbl && lbl.classList.contains("rail-settings-group")) group = lbl.textContent.trim();
        return { label: label, href: a.getAttribute("href"), sec: sec, group: group };
      });

    var root = document.createElement("div");
    root.className = "scon-pal";
    root.setAttribute("role", "dialog");
    root.setAttribute("aria-modal", "true");
    root.setAttribute("aria-label", "Пошук налаштування");
    root.innerHTML =
      '<div class="scon-pal-box">' +
      '  <div class="scon-pal-top">' +
      '    <svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>' +
      '    <input type="search" placeholder="Що налаштувати? напр. «пошта», «export», «оновлення»" autocomplete="off">' +
      "  </div>" +
      '  <ul class="scon-pal-list"></ul>' +
      '  <div class="scon-pal-foot"><span>↑ ↓ вибір</span><span>Enter відкрити</span><span>Esc закрити</span></div>' +
      "</div>";

    var input = root.querySelector("input");
    var list = root.querySelector(".scon-pal-list");
    var rows = [];
    var sel = 0;

    function render(q) {
      q = (q || "").trim().toLowerCase();
      rows = entries.filter(function (en) {
        return !q || (en.label + " " + en.group).toLowerCase().indexOf(q) !== -1;
      });
      sel = 0;
      if (!rows.length) {
        list.innerHTML = '<li class="scon-pal-empty">Нічого не знайдено</li>';
        return;
      }
      list.innerHTML = rows
        .map(function (en, i) {
          return (
            "<li" + (i === 0 ? ' aria-selected="true"' : "") + '><button type="button" data-i="' + i + '">' +
            "<span>" + escapeHtml(en.label) + "</span>" +
            (en.group ? '<span class="g">' + escapeHtml(en.group) + "</span>" : "") +
            "</button></li>"
          );
        })
        .join("");
      Array.prototype.forEach.call(list.querySelectorAll("button"), function (b) {
        b.addEventListener("click", function () { pick(+b.dataset.i); });
      });
    }
    function mark() {
      Array.prototype.forEach.call(list.children, function (li, i) {
        if (i === sel) { li.setAttribute("aria-selected", "true"); li.scrollIntoView({ block: "nearest" }); }
        else li.removeAttribute("aria-selected");
      });
    }
    function pick(i) {
      var en = rows[i];
      if (!en) return;
      close();
      if (en.sec && sectionKeys().indexOf(en.sec) !== -1) show(en.sec);
      else window.location.href = en.href;
    }
    function open() {
      root.classList.add("is-open");
      input.value = "";
      render("");
      window.setTimeout(function () { input.focus(); }, 20);
    }
    function close() { root.classList.remove("is-open"); }

    input.addEventListener("input", function () { render(input.value); });
    input.addEventListener("keydown", function (e) {
      if (e.key === "ArrowDown") { e.preventDefault(); sel = Math.min(sel + 1, rows.length - 1); mark(); }
      else if (e.key === "ArrowUp") { e.preventDefault(); sel = Math.max(sel - 1, 0); mark(); }
      else if (e.key === "Enter") { e.preventDefault(); pick(sel); }
    });
    root.addEventListener("click", function (e) { if (e.target === root) close(); });

    return { root: root, open: open, close: close, isOpen: function () { return root.classList.contains("is-open"); } };
  }

  document.querySelectorAll("[data-scon-palette]").forEach(function (b) {
    b.addEventListener("click", function () { pal.open(); });
  });
  document.addEventListener("keydown", function (e) {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") { e.preventDefault(); pal.open(); }
    if (e.key === "Escape" && pal.isOpen()) pal.close();
  });

  // ── copy-to-clipboard (service-account address) ──────────
  document.querySelectorAll("[data-copy-target]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var el = document.querySelector(btn.dataset.copyTarget);
      if (!el) return;
      var text = el.textContent.trim();
      var done = function () {
        var was = btn.textContent;
        btn.textContent = "Скопійовано";
        window.setTimeout(function () { btn.textContent = was; }, 1600);
        if (window.showToast) window.showToast("Адресу скопійовано", "success");
      };
      if (navigator.clipboard) navigator.clipboard.writeText(text).then(done, done);
      else done();
    });
  });

  // ── self-check (Стан системи) ────────────────────────────
  var diag = main.querySelector("[data-selfcheck]");
  if (diag) initSelfCheck(diag);

  function initSelfCheck(root) {
    var runBtn = root.querySelector("[data-diag-run]");
    var copyBtn = root.querySelector("[data-diag-copy]");
    var listEl = root.querySelector("[data-diag-list]");
    var sumEl = root.querySelector("[data-diag-sum]");
    var mapLinks = Array.prototype.slice.call(main.querySelectorAll(".scon-map .scon-link"));
    var mapNodes = Array.prototype.slice.call(main.querySelectorAll(".scon-map .scon-node"));
    var report = [];

    function svgFor(c) {
      if (!c.ok) return '<svg class="cross" viewBox="0 0 24 24"><path d="M6 6l12 12M18 6 6 18"/></svg>';
      if (c.warn) return '<svg class="bang" viewBox="0 0 24 24"><path d="M12 7v6M12 17h.01"/></svg>';
      return '<svg class="tick" viewBox="0 0 24 24"><path d="m4 12.5 5 5L20 7"/></svg>';
    }

    // The endpoint streams NDJSON — one line per probe, emitted the moment
    // that probe returns, then a final {done,...}. So a row's spinner means
    // "this check is running right now", not a staged replay of a finished run.
    runBtn.addEventListener("click", function () {
      runBtn.disabled = true;
      runBtn.textContent = "Перевіряю…";
      copyBtn.hidden = true;
      sumEl.textContent = "виконується…";
      report = [];
      listEl.hidden = false;
      listEl.innerHTML = "";
      mapLinks.forEach(function (l) { l.classList.remove("is-run"); });
      mapNodes.forEach(function (n) { n.classList.remove("is-hot"); });

      var index = 0;
      var rows = [];

      fetch("/settings/selfcheck", {
        method: "POST",
        headers: { "X-Requested-With": "fetch" },
      })
        .then(function (r) {
          if (!r.ok) throw new Error("HTTP " + r.status);
          if (!r.body || !r.body.getReader) throw new Error("no stream");
          return pump(r.body.getReader());
        })
        .catch(fail);

      function pump(reader) {
        var decoder = new TextDecoder();
        var buf = "";
        return reader.read().then(function step(res) {
          if (res.done) {
            if (buf.trim()) handle(JSON.parse(buf));
            return;
          }
          buf += decoder.decode(res.value, { stream: true });
          var lines = buf.split("\n");
          buf = lines.pop();
          lines.forEach(function (line) {
            if (line.trim()) handle(JSON.parse(line));
          });
          return reader.read().then(step);
        });
      }

      function handle(msg) {
        if (msg.steps) { render(msg.steps); markRunning(0); return; }
        if (msg.done) { finish(msg); return; }
        settleRow(index, msg);
        index += 1;
        markRunning(index);
      }

      function render(steps) {
        listEl.innerHTML = steps
          .map(function (s) {
            return '<li data-key="' + escapeHtml(s.key) + '"><span class="st"></span>' +
              '<span class="nm">' + escapeHtml(s.name) + "</span>" +
              '<span class="dt"></span><span class="ms"></span></li>';
          })
          .join("");
        rows = Array.prototype.slice.call(listEl.children);
      }

      function markRunning(i) {
        var li = rows[i];
        if (!li) return;
        li.className = "is-run";
        li.querySelector(".st").innerHTML = '<span class="spin"></span>';
        if (mapLinks[i]) mapLinks[i].classList.add("is-run");
        if (mapNodes[i]) mapNodes[i].classList.add("is-hot");
      }

      function settleRow(i, c) {
        var li = rows[i];
        if (!li) return;
        li.className = c.ok ? (c.warn ? "is-done warn" : "is-done ok") : "is-done bad";
        li.querySelector(".st").innerHTML = svgFor(c);
        li.querySelector(".nm").textContent = c.name;
        li.querySelector(".dt").textContent = c.detail || "";
        li.querySelector(".ms").textContent = c.ms + " мс";
        if (mapNodes[i + 1]) mapNodes[i + 1].classList.add("is-hot");
        report.push(
          (c.ok ? (c.warn ? "[WARN] " : "[ OK ] ") : "[FAIL] ") +
          c.name + " — " + (c.detail || "") + " (" + c.ms + " мс)"
        );
      }

      function fail() {
        runBtn.disabled = false;
        runBtn.textContent = "Запустити самоперевірку";
        sumEl.textContent = "не вдалося виконати";
        if (window.showToast) window.showToast("Самоперевірка не виконалась", "error");
      }
    });

    function finish(data) {
      runBtn.disabled = false;
      runBtn.textContent = "Перезапустити";
      copyBtn.hidden = false;
      sumEl.textContent = data.passed + "/" + data.total + " пройдено";
      var failed = data.total - data.passed;
      if (window.showToast) {
        window.showToast(
          failed ? failed + " перевірка не пройшла" : "Самоперевірка пройдена",
          failed ? "error" : "success"
        );
      }
    }

    copyBtn.addEventListener("click", function () {
      var text = "Order Desk · самоперевірка " + new Date().toLocaleString("uk-UA") + "\n" + report.join("\n");
      if (navigator.clipboard) navigator.clipboard.writeText(text);
      if (window.showToast) window.showToast("Звіт скопійовано", "success");
    });
  }

  // ── helpers ──────────────────────────────────────────────
  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  // ── boot ─────────────────────────────────────────────────
  var start = (location.hash || "").replace("#", "");
  show(sectionKeys().indexOf(start) !== -1 ? start : "state");
  window.addEventListener("hashchange", function () {
    var k = (location.hash || "").replace("#", "");
    if (sectionKeys().indexOf(k) !== -1) show(k);
  });
})();
