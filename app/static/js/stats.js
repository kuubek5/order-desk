// «Статистика» (stats.html): бічна панель, підказки, перемикач порівняння й
// погоджений власником рух (12.10.26):
//   1) числа KPI лічать від старого значення до нового (0.6 с), стрілка — в кінці;
//   2) стовпчики при зміні періоду/фільтра ПЕРЕТІКАЮТЬ до нової висоти (FLIP:
//      лише transform), а не малюються з нуля;
//   3) клікнутий стовпчик підсвічується, панель виїжджає з боку кліку;
//   5) «було» (риски, попереднє середнє) з'являється плавно (CSS, клас cmp-on).
// prefers-reduced-motion — одразу кінцевий стан. Петель немає.
// Вміст #sx-root свапає HTMX (вкладки, період, фільтри), тому всі слухачі
// делеговані з document.
(function () {
  "use strict";

  const ROOT = "#sx-root";
  const DUR = 600;
  const reduce = () => window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const root = () => document.querySelector(ROOT);

  // ── числа ──────────────────────────────────────────────────────────────
  function fmt(value, dec) {
    const fixed = Math.abs(value).toFixed(dec);
    const parts = fixed.split(".");
    parts[0] = parts[0].replace(/\B(?=(\d{3})+(?!\d))/g, " ");
    return (value < 0 ? "−" : "") + parts.join(",");
  }

  function countAll(scope, fromMap) {
    const nums = Array.from(scope.querySelectorAll(".sx-num[data-v]"));
    if (!nums.length) return;
    const host = scope.querySelector(".sx-kpis") || scope;
    if (reduce()) return;
    host.classList.add("sx-counting");
    const start = performance.now();
    const jobs = nums.map((el, i) => {
      const to = parseFloat(el.dataset.v);
      let from = fromMap && fromMap.has(i) ? fromMap.get(i) : parseFloat(el.dataset.was);
      if (!isFinite(from)) from = 0;
      return { el, to, from, dec: parseInt(el.dataset.dec || "0", 10) };
    }).filter((j) => isFinite(j.to) && j.from !== j.to);
    if (!jobs.length) { host.classList.remove("sx-counting"); return; }
    function tick(now) {
      const t = Math.min(1, (now - start) / DUR);
      const e = 1 - Math.pow(1 - t, 3);
      jobs.forEach((j) => { j.el.textContent = fmt(j.from + (j.to - j.from) * e, j.dec); });
      if (t < 1) requestAnimationFrame(tick);
      else host.classList.remove("sx-counting");
    }
    requestAnimationFrame(tick);
  }

  function readNums(scope) {
    const map = new Map();
    scope.querySelectorAll(".sx-num[data-v]").forEach((el, i) => {
      const v = parseFloat((el.textContent || "").replace(/[\s ]/g, "").replace(",", ".").replace("−", "-"));
      if (isFinite(v)) map.set(i, v);
    });
    return map;
  }

  // ── стовпчики (FLIP) ───────────────────────────────────────────────────
  function readBars(scope) {
    const out = {};
    scope.querySelectorAll("svg[data-chart]").forEach((svg) => {
      const id = svg.dataset.chart;
      const byKey = new Map();
      const byIdx = [];
      svg.querySelectorAll(".sx-bar").forEach((bar) => {
        const stack = bar.querySelector(".sx-stack");
        let h = 0;
        try { h = stack ? stack.getBBox().height : 0; } catch (e) { h = 0; }
        byKey.set(bar.dataset.key, h);
        byIdx.push(h);
      });
      out[id] = { byKey, byIdx };
    });
    return out;
  }

  function morphBars(scope, before) {
    if (!before || reduce()) return;
    scope.querySelectorAll("svg[data-chart]").forEach((svg) => {
      const old = before[svg.dataset.chart];
      if (!old) return;
      svg.querySelectorAll(".sx-bar").forEach((bar, i) => {
        const stack = bar.querySelector(".sx-stack");
        if (!stack) return;
        let h = 0;
        try { h = stack.getBBox().height; } catch (e) { return; }
        if (!(h > 0)) return;
        let was = old.byKey.has(bar.dataset.key) ? old.byKey.get(bar.dataset.key) : old.byIdx[i];
        if (was === undefined) return;
        const s = Math.max(0, was / h);
        if (Math.abs(s - 1) < 0.01) return;
        stack.style.transition = "none";
        stack.style.transform = "scaleY(" + s.toFixed(4) + ")";
        void stack.getBoundingClientRect();
        stack.style.transition = "transform " + DUR + "ms cubic-bezier(.2,.7,.2,1)";
        stack.style.transform = "";
        stack.addEventListener("transitionend", () => { stack.style.transition = ""; }, { once: true });
      });
    });
  }

  let captured = null;
  document.addEventListener("htmx:beforeSwap", (evt) => {
    const target = evt.detail && evt.detail.target;
    if (!target || target.id !== "sx-root") return;
    captured = { nums: readNums(target), bars: readBars(target) };
  });
  document.addEventListener("htmx:afterSettle", (evt) => {
    const r = root();
    if (!r || !captured) return;
    const tgt = evt.detail && evt.detail.target;
    if (tgt && tgt.id !== "sx-root" && !(tgt.contains && tgt.contains(r))) return;
    const prev = captured;
    captured = null;
    morphBars(r, prev.bars);
    countAll(r, prev.nums);
  });

  // ── бічна панель ───────────────────────────────────────────────────────
  function listUrl(extra) {
    const r = root();
    const qs = (r && r.dataset.qs) || "";
    return "/stats/list" + qs + (qs ? "&" : "?") + extra;
  }

  function openSide(fromX) {
    const side = document.getElementById("sx-side");
    if (!side) return;
    const rect = side.getBoundingClientRect();
    const dir = typeof fromX === "number" && fromX < rect.left ? -1 : 1;
    side.style.setProperty("--sx-from", String(dir));
    side.classList.add("is-entering", "is-open", "has-data");
    void side.offsetWidth;
    side.classList.remove("is-entering");
  }

  function closeSide() {
    const side = document.getElementById("sx-side");
    if (side) side.classList.remove("is-open");
    document.querySelectorAll(".sx-cell.is-sel").forEach((c) => c.classList.remove("is-sel"));
  }

  let sideCtl = null;
  function loadSide(extra, fromX) {
    const body = document.getElementById("sx-side-body");
    if (!body) return;
    if (sideCtl) sideCtl.abort();
    const ctl = new AbortController();
    sideCtl = ctl;
    const timer = setTimeout(() => ctl.abort(), 10000);
    body.classList.add("is-loading");
    fetch(listUrl(extra), { headers: { "HX-Request": "true" }, credentials: "same-origin", signal: ctl.signal })
      .then((resp) => {
        const redirect = resp.headers.get("HX-Redirect");
        if (redirect) { window.location.href = redirect; return null; }
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        return resp.text();
      })
      .then((html) => {
        if (html === null) return;
        body.innerHTML = html;
        openSide(fromX);
      })
      .catch((err) => {
        if (err && err.name === "AbortError" && sideCtl !== ctl) return;
        body.innerHTML = '<div class="sx-sh"><div class="sx-sh-t"><h3>Не вдалося</h3><button type="button" class="sx-x sx-close" data-side-close aria-label="Закрити">✕</button></div></div><div class="sx-empty sx-side-empty">Список не завантажився. Спробуйте ще раз.</div>';
        openSide(fromX);
      })
      .finally(() => { clearTimeout(timer); body.classList.remove("is-loading"); });
  }

  function selectBar(key) {
    document.querySelectorAll(ROOT + " .sx-days .sx-bar").forEach((b) => {
      b.classList.toggle("is-sel", b.dataset.key === key);
    });
  }

  function activate(el, x) {
    if (el.matches(".sx-bar[data-day][role=button]")) {
      selectBar(el.dataset.day);
      loadSide("day=" + encodeURIComponent(el.dataset.day), x);
      return true;
    }
    if (el.matches(".sx-cell[data-cell]")) {
      document.querySelectorAll(".sx-cell.is-sel").forEach((c) => c.classList.remove("is-sel"));
      el.classList.add("is-sel");
      loadSide("cell=" + encodeURIComponent(el.dataset.cell), x);
      return true;
    }
    return false;
  }

  document.addEventListener("click", (evt) => {
    const t = evt.target;
    if (!(t instanceof Element) || !t.closest(ROOT)) return;
    const hit = t.closest(".sx-bar[data-day][role=button], .sx-cell[data-cell]");
    if (hit && activate(hit, evt.clientX)) return;
    const opener = t.closest("[data-side-open]");
    if (opener) {
      evt.preventDefault();
      loadSide(opener.getAttribute("data-side-open"), evt.clientX);
      return;
    }
    if (t.closest("[data-side-close]")) {
      evt.preventDefault();
      closeSide();
      return;
    }
    const more = t.closest("[data-more]");
    if (more) {
      const list = more.closest(".sx-card").querySelector(".sx-clients");
      const on = list.classList.toggle("show-all");
      more.textContent = on ? "менше ←" : "усі →";
      return;
    }
    const cmp = t.closest("[data-cmp-toggle]");
    if (cmp) {
      evt.preventDefault();
      toggleCompare(cmp);
    }
  });

  document.addEventListener("keydown", (evt) => {
    if (evt.key === "Escape") { closeSide(); return; }
    if (evt.key !== "Enter" && evt.key !== " ") return;
    const t = evt.target;
    if (!(t instanceof Element) || !t.closest(ROOT)) return;
    const r = t.getBoundingClientRect();
    if (activate(t, r.left)) evt.preventDefault();
  });

  // ── порівняння: лише клас і адреса, без запиту ─────────────────────────
  function withCmp(url, on) {
    try {
      const u = new URL(url, window.location.origin);
      if (on) u.searchParams.delete("cmp"); else u.searchParams.set("cmp", "0");
      return u.pathname + (u.search || "");
    } catch (e) { return url; }
  }

  function toggleCompare(link) {
    const r = root();
    if (!r) return;
    const on = !r.classList.contains("cmp-on");
    r.classList.toggle("cmp-on", on);
    link.setAttribute("aria-checked", on ? "true" : "false");
    link.setAttribute("href", withCmp(link.getAttribute("href"), !on));
    r.querySelectorAll("a[href^='/stats']").forEach((a) => {
      if (a === link) return;
      a.setAttribute("href", withCmp(a.getAttribute("href"), on));
      if (a.hasAttribute("hx-get")) a.setAttribute("hx-get", withCmp(a.getAttribute("hx-get"), on));
    });
    r.querySelectorAll("form").forEach((form) => {
      let input = form.querySelector("input[name=cmp]");
      if (on && input) input.remove();
      if (!on && !input) {
        input = document.createElement("input");
        input.type = "hidden"; input.name = "cmp"; input.value = "0";
        form.appendChild(input);
      }
    });
    r.dataset.qs = withCmp("/stats" + (r.dataset.qs || ""), on).replace(/^\/stats/, "");
    history.replaceState(history.state, "", withCmp(window.location.pathname + window.location.search, on));
  }

  // ── підказка ───────────────────────────────────────────────────────────
  let tipEl = null;
  document.addEventListener("pointermove", (evt) => {
    const t = evt.target;
    const host = t instanceof Element ? t.closest(ROOT + " [data-tip]") : null;
    if (!tipEl) tipEl = document.getElementById("sx-tip");
    if (!tipEl) return;
    if (!host || evt.pointerType === "touch") { tipEl.hidden = true; return; }
    tipEl.textContent = host.getAttribute("data-tip");
    tipEl.hidden = false;
    const w = tipEl.offsetWidth, h = tipEl.offsetHeight;
    let x = evt.clientX + 14, y = evt.clientY + 16;
    if (x + w > window.innerWidth - 8) x = evt.clientX - w - 14;
    if (y + h > window.innerHeight - 8) y = evt.clientY - h - 12;
    tipEl.style.left = x + "px";
    tipEl.style.top = y + "px";
  });
  document.addEventListener("pointerleave", () => { if (tipEl) tipEl.hidden = true; });

  // ── перший показ ───────────────────────────────────────────────────────
  function boot() {
    const r = root();
    if (r) countAll(r, null);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
