// «Статистика» (stats.html): бічна панель, підказки, перемикач порівняння й
// погоджений власником рух (12.10.26):
//   1) числа KPI лічать від старого значення до нового (0.6 с), стрілка — в кінці;
//   2) стовпчики при зміні періоду/фільтра ПЕРЕТІКАЮТЬ до нової висоти (FLIP:
//      лише transform), а не малюються з нуля;
//   3) клікнутий стовпчик підсвічується, панель виїжджає з боку кліку;
//   5) «було» (риски, попереднє середнє) з'являється плавно (CSS, клас cmp-on);
//   6) перемикачі мають спільну плашку, що їде за кліком ще до відповіді сервера;
//   7) при зміні вкладки картки підіймаються по черзі, стовпчики ростуть.
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

  // Перший показ графіка: стовпчики ростуть від осі хвилею зліва направо.
  function growBars(svg) {
    svg.querySelectorAll(".sx-bar .sx-stack").forEach((stack, i) => {
      const delay = Math.min(i * 16, 360);
      stack.style.transition = "none";
      stack.style.transform = "scaleY(0)";
      void stack.getBoundingClientRect();
      stack.style.transition = "transform " + DUR + "ms cubic-bezier(.2,.7,.2,1) " + delay + "ms";
      stack.style.transform = "";
      stack.addEventListener("transitionend", () => { stack.style.transition = ""; }, { once: true });
    });
  }

  function morphBars(scope, before) {
    if (reduce()) return;
    scope.querySelectorAll("svg[data-chart]").forEach((svg) => {
      const old = before && before[svg.dataset.chart];
      if (!old) { growBars(svg); return; }
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

  // ── текст у графіках ───────────────────────────────────────────────────
  // SVG масштабується під ширину колонки разом зі шрифтом. --k повертає тексту
  // фізичний розмір: скільки одиниць viewBox припадає на один піксель.
  const chartObserver = "ResizeObserver" in window ? new ResizeObserver((items) => {
    items.forEach((item) => setChartScale(item.target));
  }) : null;

  function setChartScale(svg) {
    const vb = svg.viewBox && svg.viewBox.baseVal;
    const w = svg.clientWidth;
    if (vb && vb.width && w) svg.style.setProperty("--k", (vb.width / w).toFixed(3));
  }

  function scaleCharts(scope) {
    scope.querySelectorAll("svg.sx-chart").forEach((svg) => {
      setChartScale(svg);
      if (chartObserver) chartObserver.observe(svg);
    });
  }

  // ── плашка перемикачів ─────────────────────────────────────────────────
  function pillTarget(box) {
    return box.querySelector(":scope > [aria-current], :scope > .sx-custom.on > summary, :scope > .on");
  }

  function placePill(box, el, animate) {
    const pill = box.querySelector(":scope > .sx-pill");
    if (!pill) return;
    if (!el) { pill.style.opacity = "0"; return; }
    const boxRect = box.getBoundingClientRect();
    const rect = el.getBoundingClientRect();
    if (!rect.width) return;
    if (!animate) pill.style.transition = "none";
    pill.style.opacity = "1";
    pill.style.width = rect.width + "px";
    pill.style.height = rect.height + "px";
    const left = rect.left - boxRect.left - box.clientLeft + box.scrollLeft;
    const top = rect.top - boxRect.top - box.clientTop;
    pill.style.transform = "translate(" + left + "px," + top + "px)";
    if (!animate) { void pill.offsetWidth; pill.style.transition = ""; }
  }

  function readPills(scope) {
    const out = {};
    scope.querySelectorAll("[data-pills]").forEach((box, i) => {
      const el = pillTarget(box);
      if (!el) return;
      const boxRect = box.getBoundingClientRect();
      const rect = el.getBoundingClientRect();
      out[box.className.split(" ")[0] + i] = { x: rect.left - boxRect.left - box.clientLeft + box.scrollLeft, y: rect.top - boxRect.top - box.clientTop, w: rect.width, h: rect.height };
    });
    return out;
  }

  // Після свапу плашка стартує там, де стояла стара, і їде на нове місце.
  function setupPills(scope, before) {
    scope.querySelectorAll("[data-pills]").forEach((box, i) => {
      box.classList.add("sx-pilled");
      let pill = box.querySelector(":scope > .sx-pill");
      if (!pill) {
        pill = document.createElement("span");
        pill.className = "sx-pill";
        pill.setAttribute("aria-hidden", "true");
        box.insertBefore(pill, box.firstChild);
      }
      const old = before && before[box.className.split(" ")[0] + i];
      if (old && !reduce()) {
        pill.style.transition = "none";
        pill.style.width = old.w + "px";
        pill.style.height = old.h + "px";
        pill.style.transform = "translate(" + old.x + "px," + old.y + "px)";
        void pill.offsetWidth;
        pill.style.transition = "";
        placePill(box, pillTarget(box), true);
      } else {
        placePill(box, pillTarget(box), false);
      }
    });
  }

  // Клік: плашка їде одразу, не чекаючи сервера. Збій запиту повертає її назад.
  document.addEventListener("click", (evt) => {
    const t = evt.target;
    if (!(t instanceof Element) || evt.button !== 0 || evt.ctrlKey || evt.metaKey || evt.shiftKey) return;
    const link = t.closest("[data-pills] > a[hx-get]");
    if (!link) return;
    placePill(link.parentElement, link, !reduce());
  }, true);

  function resetPills() {
    const r = root();
    if (r) r.querySelectorAll("[data-pills]").forEach((box) => placePill(box, pillTarget(box), !reduce()));
  }
  document.addEventListener("htmx:responseError", resetPills);
  document.addEventListener("htmx:sendError", resetPills);
  document.addEventListener("htmx:timeout", resetPills);
  window.addEventListener("resize", () => {
    const r = root();
    if (r) r.querySelectorAll("[data-pills]").forEach((box) => placePill(box, pillTarget(box), false));
  });

  // ── поява вкладки ──────────────────────────────────────────────────────
  let enterTimer = null;
  function enterCards(r) {
    if (reduce()) return;
    Array.from(r.querySelectorAll(".sx-card:not(.sx-side)")).forEach((card, i) => {
      card.style.setProperty("--i", String(Math.min(i, 12)));
    });
    r.classList.add("sx-enter");
    clearTimeout(enterTimer);
    enterTimer = setTimeout(() => r.classList.remove("sx-enter"), 1100);
  }

  let captured = null;
  document.addEventListener("htmx:beforeSwap", (evt) => {
    const target = evt.detail && evt.detail.target;
    if (!target || target.id !== "sx-root") return;
    closePick(false);
    captured = { nums: readNums(target), bars: readBars(target), pills: readPills(target), tab: target.dataset.tab };
  });
  document.addEventListener("htmx:afterSettle", (evt) => {
    const r = root();
    if (!r || !captured) return;
    const tgt = evt.detail && evt.detail.target;
    if (tgt && tgt.id !== "sx-root" && !(tgt.contains && tgt.contains(r))) return;
    const prev = captured;
    captured = null;
    scaleCharts(r);
    setupPills(r, prev.pills);
    enhancePicks(r);
    applyDayView(r);
    if (prev.tab !== r.dataset.tab) enterCards(r);
    morphBars(r, prev.tab === r.dataset.tab ? prev.bars : null);
    countAll(r, prev.nums);
  });

  // Назад у браузері: htmx повертає знімок сторінки, тож слухачі розміру треба поновити.
  document.addEventListener("htmx:historyRestore", () => {
    const r = root();
    if (!r) return;
    r.classList.remove("sx-enter");
    scaleCharts(r);
    setupPills(r, null);
    enhancePicks(r);
  });

  // ── вибір зі списком і пошуком (фільтри) ───────────────────────────────
  // Звичайний <select> лишається у формі: саме він несе значення, а без цього
  // скрипта працює як є. Довгий перелік клієнтів без пошуку незручний.
  const CHEV = '<svg class="sx-ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m6 9 6 6 6-6"/></svg>';
  const CHECK = '<svg class="sx-ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m5 12.5 4.5 4.5L19 7.5"/></svg>';
  let pick = null;

  function enhancePicks(scope) {
    scope.querySelectorAll(".sx-pick select").forEach((sel) => {
      if (sel.dataset.enh) return;
      sel.dataset.enh = "1";
      const label = sel.closest("label");
      const name = label.querySelector("span");
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "sx-pick-btn";
      btn.setAttribute("aria-haspopup", "listbox");
      btn.setAttribute("aria-expanded", "false");
      btn.setAttribute("aria-label", sel.getAttribute("aria-label") || "");
      const nm = document.createElement("span");
      nm.textContent = name ? name.textContent : "";
      const val = document.createElement("b");
      val.textContent = sel.options[sel.selectedIndex] ? sel.options[sel.selectedIndex].textContent : "";
      btn.append(nm, val);
      btn.insertAdjacentHTML("beforeend", CHEV);
      label.hidden = true;
      label.parentElement.insertBefore(btn, label);
    });
  }

  function closePick(refocus) {
    if (!pick) return;
    const { pop, btn } = pick;
    pick = null;
    pop.remove();
    btn.setAttribute("aria-expanded", "false");
    if (refocus && btn.isConnected) btn.focus({ preventScroll: true });
  }

  function choose(sel, btn, value) {
    if (sel.value !== value) {
      sel.value = value;
      const val = btn.querySelector("b");
      if (val && sel.options[sel.selectedIndex]) val.textContent = sel.options[sel.selectedIndex].textContent;
      closePick(false);
      sel.dispatchEvent(new Event("change", { bubbles: true }));
      return;
    }
    closePick(true);
  }

  function renderOptions(list, sel, btn, needle) {
    list.textContent = "";
    const q = needle.trim().toLowerCase();
    let shown = 0;
    Array.from(sel.options).forEach((opt) => {
      if (q && !opt.textContent.toLowerCase().includes(q)) return;
      const row = document.createElement("button");
      row.type = "button";
      row.className = "sx-pop-o";
      row.setAttribute("role", "option");
      row.tabIndex = -1;
      row.dataset.value = opt.value;
      const label = document.createElement("span");
      label.textContent = opt.textContent;
      row.append(label);
      if (opt.value === sel.value) {
        row.classList.add("is-sel");
        row.setAttribute("aria-selected", "true");
        row.insertAdjacentHTML("beforeend", CHECK);
      }
      row.addEventListener("click", () => choose(sel, btn, opt.value));
      list.append(row);
      shown += 1;
    });
    if (!shown) {
      const none = document.createElement("div");
      none.className = "sx-pop-none";
      none.textContent = "Нічого не знайдено";
      list.append(none);
    }
    const first = list.querySelector(".sx-pop-o.is-sel") || list.querySelector(".sx-pop-o");
    if (first) first.classList.add("is-act");
    if (first && first.scrollIntoView) first.scrollIntoView({ block: "nearest" });
  }

  function moveActive(list, step) {
    const rows = Array.from(list.querySelectorAll(".sx-pop-o"));
    if (!rows.length) return;
    const at = rows.findIndex((row) => row.classList.contains("is-act"));
    const next = rows[(at + step + rows.length) % rows.length];
    rows.forEach((row) => row.classList.remove("is-act"));
    next.classList.add("is-act");
    next.scrollIntoView({ block: "nearest" });
  }

  function openPick(btn) {
    const sel = btn.parentElement.querySelector("select");
    if (!sel) return;
    closePick(false);
    const pop = document.createElement("div");
    pop.className = "sx-pop";
    const list = document.createElement("div");
    list.className = "sx-pop-list";
    list.setAttribute("role", "listbox");
    let find = null;
    if (sel.options.length > 7) {
      find = document.createElement("input");
      find.type = "search";
      find.className = "sx-pop-find";
      find.placeholder = "Пошук";
      find.setAttribute("aria-label", "Пошук у списку");
      find.addEventListener("input", () => renderOptions(list, sel, btn, find.value));
      pop.append(find);
    } else {
      pop.tabIndex = -1;
    }
    pop.append(list);
    pop.addEventListener("keydown", (evt) => {
      if (evt.key === "ArrowDown") { evt.preventDefault(); moveActive(list, 1); }
      else if (evt.key === "ArrowUp") { evt.preventDefault(); moveActive(list, -1); }
      else if (evt.key === "Enter") {
        const row = list.querySelector(".sx-pop-o.is-act");
        if (row) { evt.preventDefault(); row.click(); }
      } else if (evt.key === "Escape") { evt.preventDefault(); evt.stopPropagation(); closePick(true); }
      else if (evt.key === "Tab") { closePick(false); }
    });
    document.body.append(pop);
    renderOptions(list, sel, btn, "");
    const rect = btn.getBoundingClientRect();
    const w = pop.offsetWidth;
    const h = pop.offsetHeight;
    const left = Math.max(8, Math.min(rect.left, window.innerWidth - w - 8));
    const below = rect.bottom + 6 + h <= window.innerHeight - 8;
    pop.style.left = left + "px";
    pop.style.top = (below ? rect.bottom + 6 : Math.max(8, rect.top - h - 6)) + "px";
    if (!below) pop.style.transformOrigin = "bottom left";
    btn.setAttribute("aria-expanded", "true");
    pick = { pop, btn };
    (find || pop).focus({ preventScroll: true });
  }

  document.addEventListener("click", (evt) => {
    const t = evt.target;
    if (!(t instanceof Element)) return;
    const btn = t.closest(".sx-pick-btn");
    if (btn) {
      if (pick && pick.btn === btn) closePick(false); else openPick(btn);
      return;
    }
    if (pick && !t.closest(".sx-pop")) closePick(false);
    // Календар і робочі години закриваються кліком поза ними.
    document.querySelectorAll(ROOT + " details.sx-custom[open]").forEach((d) => {
      if (!d.contains(t)) d.open = false;
    });
  });
  window.addEventListener("scroll", () => closePick(false), { passive: true });

  // ── файл Excel: видно, що він готується, і що готовий ──────────────────
  function fileName(resp) {
    const head = resp.headers.get("Content-Disposition") || "";
    const star = head.match(/filename\*=UTF-8''([^;]+)/i);
    if (star) { try { return decodeURIComponent(star[1]); } catch (e) { /* нижче */ } }
    const plain = head.match(/filename="([^"]+)"/i);
    return plain ? plain[1] : "stats.xlsx";
  }

  function downloadFile(link) {
    if (link.classList.contains("is-busy")) return;
    link.classList.remove("is-done");
    link.classList.add("is-busy");
    fetch(link.href, { credentials: "same-origin" })
      .then((resp) => {
        const type = resp.headers.get("Content-Type") || "";
        if (!resp.ok || type.includes("text/html")) throw new Error("HTTP " + resp.status);
        return resp.blob().then((blob) => ({ blob, name: fileName(resp) }));
      })
      .then(({ blob, name }) => {
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = name;
        document.body.append(a);
        a.click();
        a.remove();
        setTimeout(() => URL.revokeObjectURL(url), 10000);
        link.classList.add("is-done");
        setTimeout(() => link.classList.remove("is-done"), 1800);
      })
      .catch(() => {
        if (window.showToast) window.showToast("Файл не вдалося підготувати. Спробуйте ще раз.", "error");
      })
      .finally(() => link.classList.remove("is-busy"));
  }

  // ── графік чи таблиця ──────────────────────────────────────────────────
  const VIEW_KEY = "statsDayView";
  function readDayView() {
    return window.KMStore && window.KMStore.get(VIEW_KEY) === "table" ? "table" : "chart";
  }

  function applyDayView(scope) {
    const card = scope.querySelector(".sx-daycard");
    if (!card) return;
    const view = readDayView();
    card.classList.toggle("show-table", view === "table");
    card.querySelectorAll("[data-view]").forEach((btn) => {
      const on = btn.dataset.view === view;
      btn.classList.toggle("on", on);
      btn.setAttribute("aria-pressed", on ? "true" : "false");
    });
  }

  // ── бічна панель ───────────────────────────────────────────────────────
  function listUrl(extra) {
    const r = root();
    const qs = (r && r.dataset.qs) || "";
    return "/stats/list" + qs + (qs ? "&" : "?") + extra;
  }

  const isDrawer = (side) => window.getComputedStyle(side).position === "fixed";
  let sideOpener = null;

  function openSide(fromX) {
    const side = document.getElementById("sx-side");
    if (!side) return;
    if (side.classList.contains("is-open")) return;
    const rect = side.getBoundingClientRect();
    const dir = typeof fromX === "number" && fromX < rect.left ? -1 : 1;
    side.style.setProperty("--sx-from", String(dir));
    side.classList.add("is-entering", "is-open", "has-data");
    void side.offsetWidth;
    side.classList.remove("is-entering");
  }

  function closeSide() {
    const side = document.getElementById("sx-side");
    const wasOpen = side && side.classList.contains("is-open");
    if (side) side.classList.remove("is-open");
    document.querySelectorAll(".sx-cell.is-sel").forEach((c) => c.classList.remove("is-sel"));
    if (wasOpen && sideOpener && sideOpener.isConnected && isDrawer(side)) sideOpener.focus({ preventScroll: true });
    sideOpener = null;
  }

  // Рядки списку заходять хвилею, але лише перші: довгий список не чекає сам на себе.
  function sideLoaded(body) {
    const rows = body.querySelectorAll(".sx-wr");
    const find = body.querySelector("[data-side-find]");
    if (find && rows.length > 12) find.hidden = false;
    if (!reduce()) {
      rows.forEach((row, i) => { if (i < 14) row.style.setProperty("--i", String(i)); });
      body.classList.add("sx-rows-in");
      setTimeout(() => body.classList.remove("sx-rows-in"), 700);
    }
    const side = document.getElementById("sx-side");
    const close = body.querySelector("[data-side-close]");
    if (side && close && isDrawer(side)) close.focus({ preventScroll: true });
  }

  function filterSide(input) {
    const q = input.value.trim().toLowerCase();
    const body = input.closest(".sx-side-body");
    if (!body) return;
    body.querySelectorAll(".sx-wr").forEach((row) => {
      row.hidden = Boolean(q) && !row.textContent.toLowerCase().includes(q);
    });
    body.querySelectorAll(".sx-grp").forEach((grp) => {
      const any = grp.querySelector(".sx-wr:not([hidden])");
      grp.hidden = !any;
      if (q && any) grp.open = true;
    });
  }
  document.addEventListener("input", (evt) => {
    const t = evt.target;
    if (t instanceof Element && t.matches("[data-side-find]")) filterSide(t);
  });

  let sideCtl = null;
  let sideLast = null;
  function loadSide(extra, fromX) {
    const body = document.getElementById("sx-side-body");
    if (!body) return;
    sideLast = { extra, fromX };
    // Шухляда відкривається одразу: людина бачить відгук на клік, а не паузу.
    const side = document.getElementById("sx-side");
    if (side && isDrawer(side) && !side.classList.contains("is-open")) {
      body.innerHTML = '<div class="sx-empty sx-side-empty">Завантажую…</div>';
      openSide(fromX);
    }
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
        sideLoaded(body);
      })
      .catch((err) => {
        if (err && err.name === "AbortError" && sideCtl !== ctl) return;
        body.innerHTML = '<div class="sx-sh"><div class="sx-sh-t"><h3>Не вдалося</h3><button type="button" class="sx-close" data-side-close aria-label="Закрити">×</button></div></div><div class="sx-empty sx-side-empty"><span>Список не завантажився.</span><button type="button" class="sx-btn" data-side-retry>Спробувати ще раз</button></div>';
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
      const extra = opener.getAttribute("data-side-open");
      if (extra.startsWith("day=")) selectBar(decodeURIComponent(extra.slice(4)));
      sideOpener = opener;
      loadSide(extra, evt.clientX);
      return;
    }
    if (t.closest("[data-side-retry]")) {
      if (sideLast) loadSide(sideLast.extra, sideLast.fromX);
      return;
    }
    const dl = t.closest("a.sx-dl");
    if (dl && evt.button === 0 && !evt.ctrlKey && !evt.metaKey) {
      evt.preventDefault();
      downloadFile(dl);
      return;
    }
    const viewBtn = t.closest("[data-view]");
    if (viewBtn) {
      if (window.KMStore) window.KMStore.set(VIEW_KEY, viewBtn.dataset.view);
      applyDayView(root());
      return;
    }
    if (t.closest("[data-side-close]")) {
      evt.preventDefault();
      closeSide();
      return;
    }
    const more = t.closest("[data-more]");
    if (more) {
      const list = more.closest(".sx-card").querySelector(".sx-clients, .sx-morable");
      const on = list.classList.toggle("show-all");
      more.classList.toggle("is-on", on);
      const label = more.firstChild;
      if (label && label.nodeType === Node.TEXT_NODE) {
        if (!more.dataset.label) more.dataset.label = label.textContent;
        label.textContent = on ? "менше" : more.dataset.label;
      }
      return;
    }
    const cmp = t.closest("[data-cmp-toggle]");
    if (cmp) {
      evt.preventDefault();
      toggleCompare(cmp);
    }
  });

  // Шухляда закривається кліком поза нею, але наступний стовпчик відкриває свій список.
  document.addEventListener("click", (evt) => {
    const t = evt.target;
    const side = document.getElementById("sx-side");
    if (!(t instanceof Element) || !side || !side.classList.contains("is-open") || !isDrawer(side)) return;
    if (!t.isConnected || side.contains(t)) return;
    if (t.closest("[data-side-open], .sx-bar[data-day][role=button], .sx-cell[data-cell], .sx-pop")) return;
    closeSide();
  });

  document.addEventListener("keydown", (evt) => {
    if (evt.key === "Escape") { closeSide(); return; }
    const t = evt.target;
    if (!(t instanceof Element) || !t.closest(ROOT)) return;
    // Стрілки ходять по стовпчиках: 22 зупинки табуляцією — забагато.
    if ((evt.key === "ArrowLeft" || evt.key === "ArrowRight") && t.matches(".sx-bar[role=button]")) {
      const bars = Array.from(t.closest("svg").querySelectorAll(".sx-bar[role=button]"));
      const next = bars[bars.indexOf(t) + (evt.key === "ArrowRight" ? 1 : -1)];
      if (next) { evt.preventDefault(); next.focus(); }
      return;
    }
    if (evt.key !== "Enter" && evt.key !== " ") return;
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
  // data-tip: «назва · число · подробиці». Перший шматок — що це, другий — число,
  // рядок «було …» відокремлений і зникає, коли порівняння вимкнено.
  // data-hint: звичайне речення-пояснення. Вміст лише через textContent.
  let tipEl = null;
  let tipHost = null;
  const tip = () => tipEl || (tipEl = document.getElementById("sx-tip"));

  function fillTip(host) {
    const box = tip();
    box.textContent = "";
    const hint = host.getAttribute("data-hint");
    box.classList.toggle("is-text", Boolean(hint));
    if (hint) { box.textContent = hint; return; }
    const r = root();
    const parts = (host.getAttribute("data-tip") || "").split(" · ");
    parts.forEach((text, i) => {
      const was = text.startsWith("було ");
      if (was && r && !r.classList.contains("cmp-on")) return;
      const line = document.createElement("div");
      line.className = i === 0 && parts.length > 1 ? "sx-tip-t" : (i === 1 ? "sx-tip-v" : (was ? "sx-tip-w" : "sx-tip-r"));
      line.textContent = text;
      box.append(line);
    });
  }

  function placeTip(x, y) {
    const box = tip();
    const w = box.offsetWidth, h = box.offsetHeight;
    let left = x + 14, top = y + 16;
    if (left + w > window.innerWidth - 8) left = x - w - 14;
    if (top + h > window.innerHeight - 8) top = y - h - 12;
    box.style.transform = "translate(" + Math.max(6, left) + "px," + Math.max(6, top) + "px)";
  }

  function hideTip() {
    const box = tip();
    if (box) box.hidden = true;
    tipHost = null;
  }

  function showTipFor(host, x, y) {
    const box = tip();
    if (!box) return;
    if (tipHost !== host) {
      fillTip(host);
      box.hidden = false;
      tipHost = host;
    }
    placeTip(x, y);
  }

  const TIP_SEL = "[data-hint], [data-tip]";
  document.addEventListener("pointermove", (evt) => {
    const t = evt.target;
    const host = t instanceof Element ? t.closest(ROOT + " [data-hint], " + ROOT + " [data-tip]") : null;
    if (!host || evt.pointerType === "touch") { if (tipHost && evt.pointerType !== "touch") hideTip(); return; }
    showTipFor(host, evt.clientX, evt.clientY);
  });
  document.addEventListener("focusin", (evt) => {
    const t = evt.target;
    if (!(t instanceof Element) || !t.matches(TIP_SEL) || !t.closest(ROOT)) return;
    if (!t.matches(":focus-visible")) return;
    const rect = t.getBoundingClientRect();
    showTipFor(t, rect.left + rect.width / 2, rect.bottom - 6);
  });
  document.addEventListener("focusout", (evt) => {
    if (evt.target === tipHost) hideTip();
  });
  // Значок пояснення лежить і всередині посилань-карток: клік не мусить вести далі.
  // На телефоні наведення немає, тому дотик показує пояснення.
  document.addEventListener("click", (evt) => {
    const t = evt.target;
    const hint = t instanceof Element ? t.closest(".sx-hint") : null;
    if (!hint) {
      if (tipHost && tipHost.matches(".sx-hint")) hideTip();
      return;
    }
    evt.preventDefault();
    evt.stopPropagation();
    if (tipHost === hint) { hideTip(); return; }
    const rect = hint.getBoundingClientRect();
    showTipFor(hint, rect.left, rect.bottom);
  }, true);
  document.addEventListener("pointerleave", hideTip);
  window.addEventListener("scroll", hideTip, { passive: true });

  // ── перший показ ───────────────────────────────────────────────────────
  function boot() {
    const r = root();
    if (!r) return;
    scaleCharts(r);
    setupPills(r, null);
    enhancePicks(r);
    applyDayView(r);
    enterCards(r);
    morphBars(r, null);
    countAll(r, null);
    // Шрифт міняє ширину вкладок: плашка мусить стати по готовому тексту.
    if (document.fonts && document.fonts.ready) {
      document.fonts.ready.then(() => {
        r.querySelectorAll("[data-pills]").forEach((box) => placePill(box, pillTarget(box), false));
      });
    }
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
