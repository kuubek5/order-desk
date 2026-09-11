// «Нові диски» (discs.html): галочки, живий кошик, вкладки, графік.
//
// Вантажиться ЛИШЕ цим екраном (extra_head). Усі слухачі делеговані на
// document: робоча зона (#dz-work) і вкладки свапаються HTMX-ом, і прямі
// слухачі після свапу були б мертві.
//
// Хто що робить:
//   * галочки дисків і змін, кнопки обсягу — лише ВИГЛЯД і вибір; що з них
//     вийде (групи, текст, прев'ю, кнопки) складає СЕРВЕР (POST /discs/basket)
//     — у буфер, у Telegram і в історію йде текст з одного джерела;
//   * дії, що свапають #dz-work, отримують від нас `on` (позначені диски) і
//     `note` (недописане), а «Надіслати»/«Замовлено» — ще й `ids` позначеного
//     У МИТЬ КЛІКУ (не з рендеру: браузер міг відновити галочки інакше).
(function () {
  "use strict";

  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));
  const toast = (msg, kind) => { if (window.showToast) window.showToast(msg, kind || "success"); };

  function work() { return document.getElementById("dz-work"); }
  function boxes() { return $$("[data-dz-id]", work() || document.createElement("div")); }
  function checkedIds() { return boxes().filter((b) => b.checked).map((b) => b.dataset.dzId); }
  function noteValue() { const n = $("[data-dz-note]"); return n ? n.value : ""; }

  // ── Вигляд вибору: підсвітка, галочка зміни, кнопки обсягу ────────────────
  function syncSelection() {
    const root = work();
    if (!root) return;
    boxes().forEach((box) => {
      const row = box.closest(".dz-disc");
      if (row) row.classList.toggle("is-on", box.checked);
    });
    $$("[data-dz-shift]", root).forEach((head) => {
      const section = head.closest("[data-dz-shiftbox]");
      const inner = $$("[data-dz-id]", section);
      const on = inner.filter((b) => b.checked).length;
      head.checked = on > 0 && on === inner.length;
      head.indeterminate = on > 0 && on < inner.length;
    });
    const chosen = new Set(checkedIds());
    $$("[data-dz-scope]", root).forEach((button) => {
      const ids = button.dataset.dzScope.split(",").filter(Boolean);
      const same = ids.length > 0 && ids.length === chosen.size && ids.every((id) => chosen.has(id));
      button.setAttribute("aria-pressed", same ? "true" : "false");
    });
  }

  // ── Живий кошик ───────────────────────────────────────────────────────────
  // fetch, а не htmx.ajax: HTMX тьмянить елемент-джерело на час запиту
  // (element-styles.css), і кошик блимав би на кожну галочку. Застарілу
  // відповідь відкидаємо — виграє останній клік.
  let liveTimer = null;
  let liveAbort = null;
  function refreshLive(delay) {
    window.clearTimeout(liveTimer);
    liveTimer = window.setTimeout(loadLive, delay === undefined ? 90 : delay);
  }
  async function loadLive() {
    if (!work()) return;
    if (liveAbort) liveAbort.abort();
    liveAbort = new AbortController();
    const body = new FormData();
    body.set("ids", checkedIds().join(","));
    body.set("note", noteValue());
    let html;
    try {
      const resp = await fetch("/discs/basket", { method: "POST", body, signal: liveAbort.signal, credentials: "same-origin" });
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      html = await resp.text();
    } catch (err) {
      if (err && err.name === "AbortError") return;
      toast("Кошик не оновився — перевірте зв'язок із застосунком", "error");
      return;
    }
    const tpl = document.createElement("template");
    tpl.innerHTML = html;
    Array.from(tpl.content.children).forEach((fresh) => {
      if (!fresh.id) return;
      const old = document.getElementById(fresh.id);
      if (!old) return;
      old.replaceWith(fresh);
      if (window.htmx) window.htmx.process(fresh);
    });
  }

  document.addEventListener("change", (event) => {
    const t = event.target;
    if (!t.closest || !t.closest("#dz-work")) return;
    if (t.matches("[data-dz-id]")) {
      syncSelection();
      refreshLive();
    } else if (t.matches("[data-dz-shift]")) {
      const section = t.closest("[data-dz-shiftbox]");
      $$("[data-dz-id]", section).forEach((b) => { b.checked = t.checked; });
      syncSelection();
      refreshLive();
    }
  });

  document.addEventListener("input", (event) => {
    if (event.target.matches && event.target.matches("[data-dz-note]")) refreshLive(280);
  });

  // Повторний клік по тому самому чипу — «(х2)», «(х3)»… замість другого
  // однакового рядка. «х» кирилична, як у рядках дисків.
  const COUNT = /^(.*?)\s*\(\s*[хx]\s*(\d+)\s*\)\s*$/i;
  function addNoteLine(line) {
    const field = $("[data-dz-note]");
    if (!field) return;
    const lines = field.value.split("\n");
    let bumped = false;
    for (let i = 0; i < lines.length; i++) {
      const raw = lines[i].trim();
      const m = raw.match(COUNT);
      const base = m ? m[1].trim() : raw;
      if (base === line) {
        const n = m ? Number(m[2]) + 1 : 2;
        lines[i] = `${line} (х${n})`;
        bumped = true;
        break;
      }
    }
    if (!bumped) {
      while (lines.length && !lines[lines.length - 1].trim()) lines.pop();
      lines.push(line);
    }
    field.value = lines.join("\n");
    refreshLive(0);
  }

  // ── Буфер ─────────────────────────────────────────────────────────────────
  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch (_e) {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.setAttribute("readonly", "");
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      let ok = false;
      try { ok = document.execCommand("copy"); } catch (_x) { ok = false; }
      ta.remove();
      return ok;
    }
  }
  function plural(n, one, few, many) {
    const m10 = n % 10, m100 = n % 100;
    if (m10 === 1 && m100 !== 11) return `${n} ${one}`;
    if (m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14)) return `${n} ${few}`;
    return `${n} ${many}`;
  }

  // ── Кліки ─────────────────────────────────────────────────────────────────
  const openOrders = new Set();

  document.addEventListener("click", async (event) => {
    const t = event.target;
    if (!t.closest) return;

    const copy = t.closest("[data-dz-copy]");
    if (copy) {
      const text = copy.dataset.copyText || "";
      if (!text) return;
      const ok = await copyText(text);
      const lines = text.split("\n").filter((l) => l.trim()).length;
      if (!ok) {
        toast("Браузер не дав доступу до буфера — виділіть текст і скопіюйте вручну", "error");
      } else if (copy.dataset.dzCopyWhat) {
        toast(`Скопійовано ${copy.dataset.dzCopyWhat}`);
      } else {
        toast(`Скопійовано ${plural(lines, "рядок", "рядки", "рядків")} замовлення`);
      }
      return;
    }

    const scope = t.closest("[data-dz-scope]");
    if (scope) {
      const ids = new Set(scope.dataset.dzScope.split(",").filter(Boolean));
      boxes().forEach((b) => { b.checked = ids.has(b.dataset.dzId); });
      syncSelection();
      refreshLive(0);
      return;
    }

    const quick = t.closest("[data-dz-quick]");
    if (quick) {
      addNoteLine(quick.dataset.dzQuick);
      return;
    }

    const tab = t.closest("[data-dz-tab]");
    if (tab) {
      showTab(tab.dataset.dzTab, true);
      return;
    }

    // Рядок історії: клік (крім кнопок) розгортає текст замовлення.
    const orow = t.closest("[data-dz-orow]");
    if (orow && (!t.closest("button") || t.closest("[data-dz-otog]"))) {
      toggleOrder(orow.dataset.dzOrow);
      return;
    }

    const day = t.closest("[data-dz-day]");
    if (day) {
      const form = $("#dz-filter");
      const input = $("[data-dz-f-day]", form);
      if (!form || !input) return;
      input.value = day.dataset.dzDay && input.value !== day.dataset.dzDay ? day.dataset.dzDay : "";
      hideTip();
      if (window.htmx) window.htmx.trigger(form, "submit");
      return;
    }

    const more = t.closest("[data-dz-more]");
    if (more) {
      const form = $("#dz-filter");
      const shown = $("[data-dz-f-shown]", form);
      if (!form || !shown) return;
      shown.value = String((Number(shown.value) || 3) + 3);
      if (window.htmx) window.htmx.trigger(form, "submit");
      return;
    }

    const pathToggle = t.closest("[data-dz-path-toggle]");
    if (pathToggle) {
      const form = $("#dz-pathform");
      if (!form) return;
      form.hidden = !form.hidden;
      pathToggle.setAttribute("aria-expanded", form.hidden ? "false" : "true");
      if (!form.hidden) { const input = $("input", form); if (input) input.focus(); }
      return;
    }

    if (t.closest("[data-dz-probe-close]")) {
      const slot = $("#dz-probe");
      if (slot) slot.innerHTML = "";
    }
  });

  function toggleOrder(id, force) {
    const row = $(`[data-dz-orow="${id}"]`);
    const text = document.getElementById(`dz-otext-${id}`);
    if (!row || !text) return;
    const open = force === undefined ? text.hidden : force;
    text.hidden = !open;
    row.classList.toggle("is-open", open);
    const tog = $("[data-dz-otog]", row);
    if (tog) tog.setAttribute("aria-expanded", open ? "true" : "false");
    if (force === undefined) { if (open) openOrders.add(id); else openOrders.delete(id); }
  }

  // ── Вкладки ───────────────────────────────────────────────────────────────
  function showTab(name, remember) {
    const root = $("[data-dz-tabs]");
    if (!root) return;
    $$("[data-dz-tab]", root).forEach((b) => {
      const on = b.dataset.dzTab === name;
      b.setAttribute("aria-selected", on ? "true" : "false");
      b.tabIndex = on ? 0 : -1;
    });
    const orders = $("#dz-tab-orders"), discs = $("#dz-tab-discs"), filter = $("#dz-filter");
    if (orders) orders.hidden = name !== "orders";
    if (discs) discs.hidden = name !== "discs";
    if (filter) filter.hidden = name !== "discs";
    if (remember) { try { if (window.KMStore) window.KMStore.set("discsTab", name); } catch (_e) { /* сховище недоступне */ } }
  }

  document.addEventListener("keydown", (event) => {
    const tab = event.target.closest && event.target.closest("[data-dz-tab]");
    if (!tab || (event.key !== "ArrowRight" && event.key !== "ArrowLeft")) return;
    const next = tab.dataset.dzTab === "orders" ? "discs" : "orders";
    showTab(next, true);
    const btn = $(`[data-dz-tab="${next}"]`);
    if (btn) btn.focus();
    event.preventDefault();
  });

  // ── Підказка графіка ──────────────────────────────────────────────────────
  function hideTip() { const tip = $("[data-dz-tip]"); if (tip) tip.hidden = true; }
  document.addEventListener("mouseover", (event) => {
    const bar = event.target.closest && event.target.closest(".dz-col");
    if (!bar) return;
    const chart = bar.closest(".dz-chart");
    const tip = chart && $("[data-dz-tip]", chart);
    if (!tip) return;
    const [day, n, z, p, o] = (bar.dataset.tip || "").split("|");
    tip.innerHTML = "";
    const head = document.createElement("div");
    head.innerHTML = `<b></b> · ${plural(Number(n), "диск", "диски", "дисків")}`;
    head.querySelector("b").textContent = day;
    const split = document.createElement("div");
    split.innerHTML = `<span class="z">■</span> Цирконій <b>${Number(z)}</b> &nbsp; <span class="p">■</span> ПММА <b>${Number(p)}</b>` +
      (Number(o) ? ` &nbsp; Інше <b>${Number(o)}</b>` : "");
    tip.append(head, split);
    const box = chart.getBoundingClientRect(), r = bar.getBoundingClientRect();
    tip.style.left = `${r.left - box.left + r.width / 2}px`;
    tip.style.top = `${r.top - box.top + 6}px`;
    tip.hidden = false;
  });
  document.addEventListener("mouseout", (event) => {
    const bar = event.target.closest && event.target.closest(".dz-col");
    if (bar && !(event.relatedTarget && event.relatedTarget.closest && event.relatedTarget.closest(".dz-col"))) hideTip();
  });

  // ── HTMX: що додати до запиту ─────────────────────────────────────────────
  document.addEventListener("htmx:configRequest", (event) => {
    const elt = event.detail.elt;
    const target = event.detail.target;
    const touchesWork = (target && target.id === "dz-work") || (elt && elt.id === "dz-work");
    if (!touchesWork) return;
    const params = event.detail.parameters;
    if (params.on === undefined) params.on = checkedIds().join(",");
    if (params.note === undefined) params.note = noteValue();
    if (elt && elt.matches && elt.matches("[data-dz-order]")) params.ids = checkedIds().join(",");
  });

  // Після свапу: вигляд вибору (indeterminate ставиться лише з JS) і
  // розгорнуті записи історії, які людина відкрила до оновлення.
  document.addEventListener("htmx:afterSettle", () => {
    syncSelection();
    openOrders.forEach((id) => toggleOrder(id, true));
  });

  function init() {
    if (!$("[data-dz-tabs]")) return;
    let tab = "orders";
    try { if (window.KMStore && window.KMStore.get("discsTab") === "discs") tab = "discs"; } catch (_e) { /* немає сховища */ }
    showTab(tab, false);
    syncSelection();
    // Браузер міг відновити галочки після «Назад» — кошик має відповідати
    // тому, що видно, а не рендеру.
    if (boxes().some((b) => b.checked) || noteValue()) refreshLive(0);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
