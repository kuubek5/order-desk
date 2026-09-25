// Тріаж пошти (CLAUDE.md §9, екран 2).
//
// Той самий двошаровий захист полла, що й у черзі: байт-у-байт відповідь —
// без підміни, інакше зберегти скрол списку (.listwrap). Порядок рядків під
// курсором оператора не має зсуватись — це писане правило видачі, і тут воно
// діє так само.

// Same two-layer poll guard for the mail triage list (#mail-list-rows, polled
// every 15s). The list scrolls inside .listwrap; an outerHTML swap of its only
// child would reset that to the top. Byte-identical response → skip swap. Most
// rows carry hx-preserve so the swap is cheap and never re-animates them; this
// just keeps the operator's scroll position when the set actually changes.
let lastMailListResponse = null;

let savedMailScroll = null;

document.addEventListener("htmx:beforeSwap", (event) => {
  const target = event.detail.target;
  if (!target || target.id !== "mail-list-rows") return;
  const incoming = event.detail.serverResponse;
  if (incoming != null && incoming === lastMailListResponse) {
    event.detail.shouldSwap = false;
    return;
  }
  lastMailListResponse = incoming;
  const wrap = target.closest(".listwrap");
  savedMailScroll = wrap ? wrap.scrollTop : null;
});

document.addEventListener("htmx:afterSettle", (event) => {
  if (savedMailScroll == null) return;
  const el = event.detail && event.detail.elt;
  if (el && el.id === "mail-list-rows") {
    const wrap = el.closest(".listwrap");
    if (wrap) wrap.scrollTop = savedMailScroll;
    savedMailScroll = null;
  }
});

// ── Сторінки списку: по 15 листів + «Показати всі» (власник 25.09.26) ────────
// Сервер віддає весь список, як і раніше; тут лише ховаємо рядки поза поточною
// сторінкою (атрибут hidden). Чому не серверні сторінки: вибір галочками живе в
// DOM (конвеєр, масові дії) і мусить переживати перехід між сторінками, а
// «Показати всі» тоді миттєве. «Показати всі» памʼятається в браузері оператора.
// ── Ctrl+клік: позначити листи, щоб зосередитись (власник 25.09.26) ─────────
// «Обробляю 2 листи з купи — хочу бачити лише їх». Ctrl (⌘ на Mac) + клік по
// рядку позначає лист (обводка з сяйвом), повторний — знімає; картку при цьому
// НЕ відкриваємо — клік із Ctrl означає «позначити», а не «відкрити». Решта
// рядків тьмяніє, поки позначено хоч один. Позначки — у браузері оператора
// (KMStore), переживають полл і F5. Світло пробігає рамкою ОДИН раз у момент
// позначення (`mail-focus-in`): петель анімації в тріажі не заводимо (§14).
window.KMMailFocus = (function () {
  const KEY = "mail.focusIds";
  const MAX = 50;
  const store = window.KMStore;  // storage.js: префікс і try/catch — там
  let ids = new Set();
  try {
    const raw = store && store.get(KEY);
    if (raw) ids = new Set(JSON.parse(raw).map(String).slice(0, MAX));
  } catch (e) {
    ids = new Set();
  }

  function save() {
    if (!store) return;
    if (ids.size) store.set(KEY, JSON.stringify(Array.from(ids).slice(-MAX)));
    else store.remove(KEY);
  }

  function apply() {
    let any = false;
    document.querySelectorAll("#mail-list-rows .mailrow").forEach((row) => {
      const on = ids.has(String(row.dataset.mailId));
      row.classList.toggle("mail-focus", on);
      if (on) any = true;
    });
    const wrap = document.querySelector(".mailv2 .listwrap");
    if (wrap) wrap.classList.toggle("has-focus", any);
  }

  function toggle(row) {
    const id = String(row.dataset.mailId || "");
    if (!id) return;
    if (ids.has(id)) {
      ids.delete(id);
      row.classList.remove("mail-focus-in");
    } else {
      ids.add(id);
      row.classList.add("mail-focus-in");
    }
    save();
    apply();
  }

  // Фаза ПЕРЕХОПЛЕННЯ на document: спрацьовує раніше за htmx-обробник кліку на
  // самому рядку, тож stopImmediatePropagation не дає картці відкритись.
  document.addEventListener("click", (event) => {
    if (!(event.ctrlKey || event.metaKey)) return;
    const row = event.target.closest && event.target.closest("#mail-list-rows .mailrow");
    if (!row) return;
    if (event.target.closest(".mailcb-wrap, form, button, a, input")) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    toggle(row);
  }, true);

  document.addEventListener("animationend", (event) => {
    if (event.animationName === "mailFocusSweep" && event.target.classList) {
      event.target.classList.remove("mail-focus-in");
    }
  });

  // Полл перемальовує рядки без hx-preserve — повернути їм позначку.
  document.addEventListener("htmx:afterSettle", (event) => {
    const el = event.detail && event.detail.elt;
    if (el && el.id === "mail-list-rows") apply();
  });
  document.addEventListener("DOMContentLoaded", apply);
  if (document.readyState !== "loading") apply();

  return { apply: apply };
})();

window.KMMailPager = (function () {
  const PAGE = 15;
  const KEY = "mail.showAll";
  const store = window.KMStore;  // storage.js: префікс ключа й try/catch — там
  let page = 0;
  let all = !!(store && store.get(KEY) === "1");

  function rows() {
    return Array.prototype.slice.call(document.querySelectorAll("#mail-list-rows .mailrow"));
  }

  function button(label, opts) {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = label;
    if (opts.title) b.title = opts.title;
    if (opts.cls) b.className = opts.cls;
    if (opts.current) b.setAttribute("aria-current", "page");
    if (opts.disabled) b.disabled = true;
    if (opts.page != null) b.dataset.page = String(opts.page);
    if (opts.act) b.dataset.act = opts.act;
    return b;
  }

  // Номери сторінок: перша, остання й сусіди поточної; решта — «…».
  function pageList(pages) {
    const out = [];
    for (let i = 0; i < pages; i++) {
      if (i === 0 || i === pages - 1 || Math.abs(i - page) <= 1) out.push(i);
      else if (out[out.length - 1] !== "…") out.push("…");
    }
    return out;
  }

  function apply() {
    const pager = document.getElementById("mail-pager");
    const list = rows();
    const pages = Math.max(1, Math.ceil(list.length / PAGE));
    if (page > pages - 1) page = pages - 1;
    list.forEach((r, i) => {
      r.hidden = !all && Math.floor(i / PAGE) !== page;
    });
    // «Обрати всі» рахує лише видиму сторінку — хай перечитає свій стан.
    document.dispatchEvent(new Event("mailPagerChange"));
    if (!pager) return;
    pager.innerHTML = "";
    if (list.length <= PAGE) {
      pager.hidden = true;
      return;
    }
    pager.hidden = false;
    if (all) {
      pager.appendChild(Object.assign(document.createElement("span"), {
        className: "mp-range", textContent: "усі " + list.length,
      }));
    } else {
      pager.appendChild(button("‹", { act: "prev", disabled: page === 0, title: "Попередня сторінка" }));
      pageList(pages).forEach((p) => {
        if (p === "…") {
          pager.appendChild(Object.assign(document.createElement("span"), { className: "mp-range", textContent: "…" }));
        } else {
          pager.appendChild(button(String(p + 1), { page: p, current: p === page }));
        }
      });
      pager.appendChild(button("›", { act: "next", disabled: page >= pages - 1, title: "Наступна сторінка" }));
      const from = page * PAGE + 1;
      const to = Math.min(list.length, (page + 1) * PAGE);
      pager.appendChild(Object.assign(document.createElement("span"), {
        className: "mp-range", textContent: from + "–" + to + " з " + list.length,
      }));
    }
    pager.appendChild(button(all ? "По 15" : "Показати всі", {
      act: "all", cls: "mp-all",
      title: all ? "Знову по 15 листів на сторінку" : "Показати всі " + list.length + " листів одним списком",
    }));
  }

  function go(p) {
    page = p;
    apply();
    const wrap = document.querySelector(".mailv2 .listwrap");
    if (wrap) wrap.scrollTop = 0;
  }

  // Показати сторінку, де лежить рядок (J/K, відкритий лист із ?open=).
  function reveal(row) {
    if (!row || all) return;
    const i = rows().indexOf(row);
    if (i >= 0 && Math.floor(i / PAGE) !== page) {
      page = Math.floor(i / PAGE);
      apply();
    }
  }

  document.addEventListener("click", (event) => {
    const b = event.target.closest("#mail-pager button");
    if (!b || b.disabled) return;
    if (b.dataset.page != null) go(parseInt(b.dataset.page, 10));
    else if (b.dataset.act === "prev") go(page - 1);
    else if (b.dataset.act === "next") go(page + 1);
    else if (b.dataset.act === "all") {
      all = !all;
      if (store) store.set(KEY, all ? "1" : "0");
      const active = document.querySelector("#mail-list-rows .mailrow.active");
      page = 0;
      if (!all && active) reveal(active);
      apply();
      if (active && active.scrollIntoView) active.scrollIntoView({ block: "nearest" });
    }
  });

  // Полл свапає #mail-list-rows: нові рядки приходять без hidden — перерахувати.
  document.addEventListener("htmx:afterSettle", (event) => {
    const el = event.detail && event.detail.elt;
    if (el && el.id === "mail-list-rows") apply();
  });

  function init() {
    const active = document.querySelector("#mail-list-rows .mailrow.active");
    if (active) page = Math.floor(rows().indexOf(active) / PAGE);
    apply();
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();

  return { reveal: reveal, apply: apply };
})();

// Mail triage: highlight the row whose letter is open in the right-hand detail
// panel. The HTMX get itself fills #mail-detail; this only tracks which row is
// the active one so the master-detail selection reads clearly.
function markMailRowActive(event) {
  const row = event.target.closest(".mailrow");
  if (!row) return;
  if (event.target.closest(".mail-reject-form")) return; // reject button isn't a selection
  document.querySelectorAll(".mailrow.active").forEach((r) => r.classList.remove("active"));
  row.classList.add("active");
  // Opening a letter clears its "unread by me" highlight instantly — the GET
  // also stamps seen_at server-side, this just keeps the UI honest before the
  // next poll/reload.
  row.classList.remove("unread");
  const dot = row.querySelector(".newdot");
  if (dot) dot.remove();
}

document.addEventListener("click", markMailRowActive);
// Рядок відкривається ще й з клавіатури (hx-trigger keyup Enter/Space у
// _mail_triage_list.html), а підсвітка жила лише на кліку: Tab+Enter відкривав
// лист, але «активним» лишався попередній рядок і крапка «непрочитано» не
// гасла — оператор не бачив, який лист він читає.
document.addEventListener("keyup", (event) => {
  if (event.key !== "Enter" && event.key !== " ") return;
  markMailRowActive(event);
});

// «Перевірити пошту» — spin + lock while the manual IMAP check runs, then a
// short cooldown (persisted in localStorage so it survives the post/redirect
// reload) keeps the button locked for a few seconds — no rapid re-spamming.
// The 2-min background auto-check is server-side and unaffected.
(function initMailSyncButton() {
  const KEY = "mailSyncAt";
  const form = document.querySelector("[data-mail-sync-form]");
  if (!form) return;
  const btn = form.querySelector(".mail-sync-btn");
  if (!btn) return;
  const label = btn.querySelector(".btn-label");
  const baseText = label ? label.textContent : "";
  const cooldownMs = (parseInt(btn.dataset.cooldown, 10) || 12) * 1000;
  let timer = null;

  function unlock() {
    if (timer) { window.clearTimeout(timer); timer = null; }
    btn.disabled = false;
    btn.classList.remove("is-cooldown", "is-syncing");
    if (label) label.textContent = baseText;
  }
  function lock(remaining) {
    btn.disabled = true;
    btn.classList.add("is-cooldown");
    (function tick() {
      if (remaining <= 0) { unlock(); return; }
      if (label) label.textContent = "Зачекайте " + Math.ceil(remaining / 1000) + " с";
      remaining -= 1000;
      timer = window.setTimeout(tick, 1000);
    })();
  }

  // Honour a cooldown left over from a recent submit (page reloaded after sync).
  const at = parseInt(KMStore.get(KEY) || "0", 10);
  const elapsed = Date.now() - at;
  if (at && elapsed >= 0 && elapsed < cooldownMs) lock(cooldownMs - elapsed);

  form.addEventListener("submit", (event) => {
    if (btn.disabled) { event.preventDefault(); return; } // locked → ignore
    KMStore.set(KEY, String(Date.now()));
    btn.classList.add("is-syncing");
    btn.disabled = true; // submission already fired; this just blocks a 2nd click
    if (label) label.textContent = "Перевіряю…";
  });
})();

// ── Лічильник файлів у рядку списку ──────────────────────────────────────
// Рядок тріажу несе hx-preserve, тому 15-секундний полл його НЕ перемальовує:
// це свідомо (щоб не рестартувати анімацію «непрочитано» і не збивати вибір),
// але побічно означає, що «N файл.» у рядку заморожене на момент завантаження
// сторінки. Після розпакування архіву чи повторного скачування список показував
// старе число, а панель — нове.
//
// Це не косметика. Оператор веде очима по СПИСКУ; якщо там написано «5 файл.»,
// а насправді їх чотири, роботу приймуть з думкою, що файл є. Сервер шле нове
// число тригером, тут воно вписується в рядок.
document.body.addEventListener("mailFilesChanged", (event) => {
  const d = (event && event.detail) || {};
  if (!d.id || typeof d.count !== "number") return;
  const row = document.getElementById("mailrow-" + d.id);
  if (!row) return;
  const att = row.querySelector(".att");
  if (!att) return;

  // Перемальовуємо ВЕСЬ сигнал: текст, тривожний клас і підказку разом.
  // Раніше писалось лише число рядків бази — і «3 з 5 файл.» перетворювалось
  // на «5 файл.», а червоний клас із підказкою лишались від старого стану.
  // Рядок казав одночасно «все є» і «чогось немає», причому число брехало
  // саме в бік «файл є» — тобто поверталась рівно та вада, від якої рядок
  // лікували.
  const rows = d.count;
  const onDisk = typeof d.on_disk === "number" ? d.on_disk : rows;
  const gone = onDisk < rows;
  att.classList.toggle("att-gone", gone);
  if (gone) {
    att.title = rows - onDisk + " файл(ів) немає на диску — відкрий лист і скачай наново";
  } else {
    att.removeAttribute("title");
  }
  // Іконка-скріпка лишається; міняється лише текстовий вузол після неї.
  const text = gone ? " " + onDisk + " з " + rows + " файл." : " " + rows + " файл.";
  for (const node of att.childNodes) {
    if (node.nodeType === Node.TEXT_NODE && node.textContent.trim()) {
      node.textContent = text;
      return;
    }
  }
});

// ── J / K по списку тріажу (аудит 05.09.26, крок 3.3) ────────────────────
//
// Тріаж — це майстер-деталь: зліва список листів, справа розкритий лист. Щоб
// переглянути десяток листів, оператор досі мусив щоразу знімати руку з
// клавіатури й цілитись мишею в рядок. J — наступний, K — попередній; далі
// вже наявні Tab/Enter і кнопки самої панелі.
//
// ДВІ РЕЧІ, ЯКІ ТУТ ЛЕГКО ЗЛАМАТИ.
//
// 1. Літера в полі вводу. Однолітерна гаряча клавіша без вартової ламає
//    набір тексту: «j» у коментарі чи в імені клієнта перемикав би лист
//    замість того, щоб надрукуватись. Умова одна на застосунок —
//    KMKeys.isTyping (palette.js); дублювати її тут не можна, бо копії
//    розходяться.
// 2. Розкладка. Фізична клавіша J в українській розкладці дає «о», K — «л».
//    Тому дивимось на event.code (позиція клавіші), і лише за його
//    відсутності — на event.key.
document.addEventListener("keydown", (event) => {
  // Модифікатори лишаємо браузеру й іншим гарячим клавішам (Ctrl+K — палітра).
  if (event.ctrlKey || event.metaKey || event.altKey || event.shiftKey) return;

  const code = event.code || "";
  const key = (event.key || "").toLowerCase();
  const forward = code === "KeyJ" || (!code && key === "j");
  const back = code === "KeyK" || (!code && key === "k");
  if (!forward && !back) return;

  if (window.KMKeys && window.KMKeys.isTyping(event.target)) return;
  // Палітра відкрита — вона зараз володіє клавіатурою.
  if (window.KMPalette && window.KMPalette.isOpen()) return;
  // Відкрите читання листа — J/K під ним перемикали б лист у картці позаду.
  if (document.querySelector("dialog.letter-reader[open]")) return;

  const rows = Array.prototype.slice.call(document.querySelectorAll(".mailrow"));
  if (!rows.length) return; // не екран тріажу — нічого не перехоплюємо

  const active = document.querySelector(".mailrow.active");
  const at = active ? rows.indexOf(active) : -1;
  // Без вибраного рядка J починає згори, K — знизу: це те, що людина мала на
  // увазі, натиснувши «вниз» чи «вгору» на щойно відкритому екрані.
  let next;
  if (at === -1) next = forward ? rows[0] : rows[rows.length - 1];
  else next = rows[Math.min(Math.max(at + (forward ? 1 : -1), 0), rows.length - 1)];
  if (!next || next === active) return;

  event.preventDefault(); // інакше пробіл/літера прогортають сторінку під панеллю
  // Наступний лист на іншій сторінці списку — перегорнути на неї.
  if (window.KMMailPager) window.KMMailPager.reveal(next);
  // preventScroll + scrollIntoView('nearest'): focus() сам би стрибнув так,
  // щоб рядок став по центру, і список смикався б на кожне натискання.
  try { next.focus({ preventScroll: true }); } catch (e) { next.focus(); }
  if (next.scrollIntoView) next.scrollIntoView({ block: "nearest" });
  // Рядок несе hx-trigger="click" — справжній клік відкриває лист у панелі й
  // заодно доводить підсвітку через markMailRowActive вище.
  next.click();
});

// ── Список файлів листа: «Розгорнути» на весь зріст (власник 25.09.26) ──────
// Вибір — вподобання оператора (KMStore), а не стан одного листа: хто
// розгорнув, той хоче бачити загальну картину й на наступному листі, і після
// автооновлення картки (скачування за посиланням перемальовує #mail-detail).
const ATTLIST_KEY = "mail.attlistWide";

function applyAttlistWide(root) {
  const wide = !!(window.KMStore && window.KMStore.get(ATTLIST_KEY) === "1");
  (root || document).querySelectorAll("[data-attlist-toggle]").forEach((btn) => {
    const list = btn.closest(".mc-atts") && btn.closest(".mc-atts").querySelector(".mc-attlist");
    if (!list) return;
    list.classList.toggle("is-wide", wide);
    btn.setAttribute("aria-expanded", wide ? "true" : "false");
    const label = btn.querySelector(".mc-attlist-label");
    if (label) label.textContent = wide ? "Згорнути" : "Розгорнути";
  });
}

document.addEventListener("click", (event) => {
  const btn = event.target.closest("[data-attlist-toggle]");
  if (!btn) return;
  const wide = btn.getAttribute("aria-expanded") !== "true";
  if (window.KMStore) window.KMStore.set(ATTLIST_KEY, wide ? "1" : "0");
  applyAttlistWide();
});
document.addEventListener("htmx:afterSettle", (event) => {
  const el = event.detail && event.detail.elt;
  if (el && el.querySelector && el.querySelector("[data-attlist-toggle]")) applyAttlistWide(el);
});
if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", () => applyAttlistWide());
else applyAttlistWide();

// ── «Що пише замовник»: розгорнути текст на місці (власник 25.09.26) ─────────
// Клік по тексту (чи «розгорнути ↓») розкриває його в тому ж блоці, як старий
// <details>; вікно читання — лише кнопкою «Читати лист». Виділення тексту
// (скопіювати телефон/адресу) блок НЕ згортає: клік, що закінчив виділення, —
// не команда.
function toggleMailBody(event) {
  const hit = event.target.closest("[data-body-toggle]");
  if (!hit) return;
  const box = hit.closest(".mc-body");
  if (!box || box.classList.contains("fits")) return;
  const sel = window.getSelection && window.getSelection();
  if (event.type === "click" && sel && String(sel).trim() && box.contains(sel.anchorNode)) return;
  const open = box.classList.toggle("is-open");
  const clip = box.querySelector(".mc-body-clip");
  if (clip) {
    clip.setAttribute("aria-expanded", open ? "true" : "false");
    if (!open) clip.scrollTop = 0;
  }
}
document.addEventListener("click", toggleMailBody);
document.addEventListener("keydown", (event) => {
  if (event.key !== "Enter" && event.key !== " ") return;
  if (!event.target.matches || !event.target.matches(".mc-body-clip[data-body-toggle]")) return;
  event.preventDefault();
  toggleMailBody(event);
});

// Текст уже влазить у 2 рядки — розгортати нічого: ховаємо підказку й курсор.
// Міряємо після кожного свапу картки (картка приходить htmx-фрагментом).
function measureMailBody(root) {
  (root || document).querySelectorAll(".mc-body").forEach((box) => {
    const clip = box.querySelector(".mc-body-clip");
    if (!clip || box.classList.contains("is-open")) return;
    box.classList.toggle("fits", clip.scrollHeight <= clip.clientHeight + 1);
  });
}
document.addEventListener("htmx:afterSettle", (event) => {
  const el = event.detail && event.detail.elt;
  if (el && el.querySelector && el.querySelector(".mc-body")) measureMailBody(el);
});
if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", () => measureMailBody());
else measureMailBody();

// ── Режим читання листа (_mail_letter_reader.html) ───────────────────────────
// Нативний <dialog>: showModal дає верхній шар, Esc і пастку фокусу. Діалог
// живе в картці, тож своп картки (інший лист, прийняття) прибирає його разом
// із нею — окремо закривати не треба.
document.addEventListener("click", (event) => {
  const open = event.target.closest("[data-letter-open]");
  if (open) {
    const card = open.closest(".mailcard");
    const dialog = card && card.querySelector("dialog.letter-reader");
    if (dialog && !dialog.open) dialog.showModal();
    return;
  }
  if (event.target.closest("[data-letter-close]")) {
    const dialog = event.target.closest("dialog.letter-reader");
    if (dialog) dialog.close();
    return;
  }
  // Клік по самому <dialog> поза вмістом — це затемнення: закрити, як у паспорті.
  if (event.target.matches && event.target.matches("dialog.letter-reader")) {
    event.target.close();
    return;
  }
  const mode = event.target.closest("[data-lr-mode]");
  if (mode) {
    const dialog = mode.closest("dialog.letter-reader");
    if (!dialog) return;
    dialog.dataset.mode = mode.dataset.lrMode;
    dialog.querySelectorAll("[data-lr-mode]").forEach((b) => {
      b.setAttribute("aria-pressed", b === mode ? "true" : "false");
    });
  }
});

// ── Підказка кольорів у картці багатокольорового листа ───────────────────────
// Клік по групі («a3.5 · денис, юшков») позначає рівно файли цієї групи й
// підставляє її матеріал. Далі — звичайне часткове прийняття однієї партії;
// після нього картка відкривається знову, і сервер показує вже решту кольорів.
// Саме нічого не обирається: група — пропозиція, оператор може зняти/додати
// галочку чи виправити матеріал перед «Прийняти» (§2, правило 3).
document.addEventListener("click", (event) => {
  const chip = event.target.closest("[data-cp-ids]");
  if (!chip) return;
  const form = chip.closest("#mail-card-form");
  if (!form) return;
  const ids = new Set((chip.dataset.cpIds || "").split(",").filter(Boolean));
  form.querySelectorAll(".mc-attcb").forEach((cb) => {
    cb.checked = ids.has(cb.value);
  });
  const material = form.querySelector("#mc-material");
  if (material && chip.dataset.cpMaterial) {
    material.value = chip.dataset.cpMaterial;
    // change — це hx-trigger рядка шляху: тека матеріалу перерахується.
    material.dispatchEvent(new Event("change", { bubbles: true }));
  }
  form.querySelectorAll("[data-cp-ids]").forEach((c) => c.classList.toggle("on", c === chip));
});

// ── Конвеєр (блок B): вибір кількох листів + батч-прийняття ──────────────────
// Стан вибору живе ЛИШЕ тут (у DOM-галочках), не в БД. Панель #mail-detail
// перемикається з картки на таблицю батчу, щойно обрано ≥1 лист, і назад на
// картку/заглушку, коли знято всі. Джерело істини — самі галочки (.mailcb), а не
// окремий Set: галочки в рядках з hx-preserve переживають 15-секундний полл, тож
// читати з DOM надійніше, ніж тримати паралельний список, який розійшовся б.

// Збирач рядків батч-таблиці у payload для POST /mail/accept-batch. Глобальний —
// його зове hx-vals='js:{payload: collectMailBatch()}' на кнопці прийняття.
window.collectMailBatch = function () {
  const rows = document.querySelectorAll("#mail-batch-form .mb-row[data-batch-id]");
  const out = [];
  rows.forEach((tr) => {
    const id = parseInt(tr.dataset.batchId, 10);
    if (!id) return;
    const val = (name) => {
      const el = tr.querySelector('[name="' + name + '"]');
      return el ? el.value : "";
    };
    out.push({
      email_id: id,
      client_name: val("client_name"),
      material_color: val("material_color"),
      quantity: val("quantity"),
      opak: val("opak"),
      sum3d_id: val("sum3d_id"),
      kind: val("kind"),
      folder_pick: val("folder_pick"),
    });
  });
  return JSON.stringify(out);
};

(function initMailBatch() {
  // Вміст панелі ДО входу в батч — щоб повернути його, коли знято всі галочки
  // (заглушка «оберіть лист» або відкрита картка). null = батч неактивний.
  let detailCache = null;

  function idsOf(selector) {
    return Array.prototype.slice
      .call(document.querySelectorAll(selector))
      .map((cb) => cb.dataset.cbId)
      .filter(Boolean);
  }

  // Усі обрані — для масових дій (/mail/bulk). Готові (data-ready) — для
  // конвеєра: у «Вхідні» галочка є на кожному листі, а приймати батчем
  // можна лише готові.
  function selectedIds() {
    return idsOf(".mailcb:checked");
  }

  function readyIds() {
    return idsOf(".mailcb[data-ready]:checked");
  }

  function isConveyor() {
    const bar = document.getElementById("mail-batchbar");
    return !!(bar && bar.dataset.conveyor);
  }

  // Пул «Обрати всі»: у «Вхідні» — лише готові, в інших вкладках — усі
  // незаблоковані. Лише ВИДИМА сторінка (як в ukr.net): «обрати всі» не має
  // тихо захоплювати листи на сторінках, яких оператор не бачить. На «Показати
  // всі» видимі — усі.
  function selectAllPool() {
    const all = document.getElementById("mail-select-all");
    return all && all.dataset.readyOnly
      ? ".mailrow:not([hidden]) .mailcb[data-ready]:not(:disabled)"
      : ".mailrow:not([hidden]) .mailcb:not(:disabled)";
  }

  function updateSelCount(ids) {
    const out = document.getElementById("mail-sel-count");
    if (out) {
      if (ids.length) {
        out.hidden = false;
        let text = "обрано " + ids.length;
        if (isConveyor()) {
          const ready = readyIds().length;
          if (ready < ids.length) text += " · готових " + ready;
        }
        out.textContent = text;
      } else {
        out.hidden = true;
      }
    }
    const form = document.getElementById("mail-bulk-form");
    if (form) form.hidden = !ids.length;
    // Лічильник стає на місце підпису «Обрати всі…» — інакше кнопки не
    // влазять в один рядок, і смуга росте під курсором (див. v2a_mail.css).
    const bar = document.getElementById("mail-batchbar");
    if (bar) bar.classList.toggle("has-sel", ids.length > 0);
    if (out) out.title = ids.length ? out.textContent : "";
    // Синхронізувати «обрати всі» з фактичним станом.
    const all = document.getElementById("mail-select-all");
    if (all) {
      const pool = selectAllPool();
      const boxes = document.querySelectorAll(pool);
      const checked = document.querySelectorAll(pool.replace(":not(:disabled)", ":not(:disabled):checked"));
      all.checked = boxes.length > 0 && checked.length === boxes.length;
      all.indeterminate = checked.length > 0 && checked.length < boxes.length;
    }
  }

  function refreshPanel() {
    updateSelCount(selectedIds());
    if (!isConveyor()) return;
    const detail = document.getElementById("mail-detail");
    if (!detail || !window.htmx) return;
    const ids = readyIds();
    if (!ids.length) {
      if (detailCache !== null) {
        detail.innerHTML = detailCache;
        detailCache = null;
        window.htmx.process(detail);
      }
      return;
    }
    if (detailCache === null) detailCache = detail.innerHTML;
    window.htmx.ajax("GET", "/mail?partial=batch&batch=" + ids.join(","), {
      target: "#mail-detail",
      swap: "innerHTML",
    });
  }

  // Галочка рядка змінилась → перебудувати панель.
  document.addEventListener("change", (event) => {
    if (event.target.classList && event.target.classList.contains("mailcb")) {
      refreshPanel();
    }
  });

  document.addEventListener("mailPagerChange", () => updateSelCount(selectedIds()));

  // «Обрати всі (готові)» — тумблер галочок свого пулу.
  document.addEventListener("change", (event) => {
    if (event.target.id !== "mail-select-all") return;
    const on = event.target.checked;
    document.querySelectorAll(selectAllPool()).forEach((cb) => {
      cb.checked = on;
    });
    refreshPanel();
  });

  // Масова дія: форма /mail/bulk — підставити id обраних і спитати підтвердження
  // (текст кнопки data-bulk-confirm, {n} = кількість). Звичайний POST із повним
  // переходом: сервер повертає в цю ж вкладку з тостом-підсумком.
  document.addEventListener("submit", (event) => {
    const form = event.target;
    if (!form || form.id !== "mail-bulk-form") return;
    const ids = selectedIds();
    if (!ids.length) {
      event.preventDefault();
      return;
    }
    const btn = event.submitter;
    const ask = btn && btn.dataset.bulkConfirm;
    if (ask && !window.confirm(ask.replace("{n}", String(ids.length)))) {
      event.preventDefault();
      return;
    }
    form.querySelector('[name="ids"]').value = ids.join(",");
    // Пункт меню «Перемістити» несе назву папки в data-folder.
    const folderInput = form.querySelector('[name="folder"]');
    if (folderInput) folderInput.value = (btn && btn.dataset.folder) || "";
    // Пункт «На уточненні» може нести ключ причини в data-reason (без нього —
    // пауза без причини).
    const reasonInput = form.querySelector('[name="reason"]');
    if (reasonInput) reasonInput.value = (btn && btn.dataset.reason) || "";
    // Повільна дія (IMAP по листу) — не дати натиснути вдруге.
    form.querySelectorAll("button").forEach((b) => {
      if (b !== btn) b.disabled = true;
    });
    if (btn) btn.textContent = "Виконую…";
  });

  // Меню «Перемістити» і «На уточнення» закриваються кліком поза ними — як
  // меню пошти.
  document.addEventListener("click", (event) => {
    document.querySelectorAll(".mb-move[open], .mc-hold[open]").forEach((menu) => {
      if (!menu.contains(event.target)) menu.removeAttribute("open");
    });
  });

  // «Зняти вибір» у шапці батч-панелі.
  document.addEventListener("click", (event) => {
    if (!event.target.closest("[data-batch-clear]")) return;
    document.querySelectorAll(".mailcb:checked").forEach((cb) => {
      cb.checked = false;
    });
    refreshPanel();
  });

  // ▸ розгортає рядок листа (текст + файли) у батч-таблиці.
  document.addEventListener("click", (event) => {
    const btn = event.target.closest("[data-batch-expand]");
    if (!btn) return;
    const id = btn.getAttribute("data-batch-expand");
    const row = document.querySelector('.mb-detailrow[data-detail-id="' + id + '"]');
    if (!row) return;
    const open = row.hasAttribute("hidden");
    if (open) row.removeAttribute("hidden");
    else row.setAttribute("hidden", "");
    btn.setAttribute("aria-expanded", open ? "true" : "false");
    btn.classList.toggle("on", open);
  });

  // Після прийняття батчу: прибрати прийняті рядки зі списку, зняти їх галочки,
  // оновити дзеркало черги. Невдалі лишаються (галочка на них уціліла), панель
  // показує результат.
  document.body.addEventListener("mailBatchDone", (event) => {
    const d = (event && event.detail) || {};
    const accepted = d.accepted || [];
    accepted.forEach((id) => {
      const row = document.getElementById("mailrow-" + id);
      if (row) row.remove();
    });
    // Панель уже свопнута сервером на результат — не тримати старий кеш картки.
    detailCache = null;
    updateSelCount(selectedIds());
    if (accepted.length && window.htmx) {
      // Прийняті роботи зʼявились у дзеркалі «Прийняте з пошти» — оновити його.
      window.htmx.ajax("GET", "/mail/queue-mirror", {
        target: "#qmir-body",
        swap: "innerHTML",
      });
    }
  });
})();

// ── Дзеркало черги внизу пошти: захист полла ─────────────────────────────────
// Тіло #qmir-body самополлиться кожні 15с (свопає вміст на свіжу таблицю). Дві
// небезпеки, як у #queue-rows черги: (1) полл під час набору затер би
// напівуведене поле; (2) без потреби перемальовував би однакову таблицю, гублячи
// скрол .tablewrap. Тому: пропускаємо своп, якщо всередині сфокусоване поле
// вводу, або якщо відповідь байт-у-байт та сама; інакше зберігаємо скрол.
let lastMirrorResponse = null;
let savedMirrorScroll = null;
document.addEventListener("htmx:beforeSwap", (event) => {
  const target = event.detail.target;
  if (!target || target.id !== "qmir-body") return;
  const ae = document.activeElement;
  if (ae && target.contains(ae) && /^(INPUT|TEXTAREA|SELECT)$/.test(ae.tagName)) {
    event.detail.shouldSwap = false; // оператор друкує — не чіпаємо
    return;
  }
  const incoming = event.detail.serverResponse;
  if (incoming != null && incoming === lastMirrorResponse) {
    event.detail.shouldSwap = false;
    return;
  }
  lastMirrorResponse = incoming;
  const wrap = target.querySelector(".tablewrap");
  savedMirrorScroll = wrap ? wrap.scrollTop : null;
});
document.addEventListener("htmx:afterSwap", (event) => {
  const target = event.detail.target;
  if (!target || target.id !== "qmir-body") return;
  if (savedMirrorScroll == null) return;
  const wrap = target.querySelector(".tablewrap");
  if (wrap) wrap.scrollTop = savedMirrorScroll;
  savedMirrorScroll = null;
});

// ── Дзеркало черги внизу пошти: згортання ────────────────────────────────────
// Кнопка в шапці перемикає клас .qmir-collapsed на секції, а CSS ховає тіло;
// стан памʼятається в KMStore. Полл тіла від класу не залежить — він свопає лише
// вміст .qmir-body, шапка з кнопкою лишаються, тож клас переживає полл.
(function () {
  const KEY = "mailMirrorCollapsed";
  const mirror = document.getElementById("mail-queue-mirror");
  if (!mirror) return; // не екран пошти
  const btn = mirror.querySelector(".qmir-toggle");

  function apply(collapsed) {
    mirror.classList.toggle("qmir-collapsed", collapsed);
    if (btn) btn.setAttribute("aria-expanded", collapsed ? "false" : "true");
  }

  let saved = "0";
  try { saved = (window.KMStore && KMStore.get(KEY)) || "0"; } catch (e) { saved = "0"; }
  apply(saved === "1");

  if (btn) {
    btn.addEventListener("click", () => {
      const collapsed = !mirror.classList.contains("qmir-collapsed");
      apply(collapsed);
      try { if (window.KMStore) KMStore.set(KEY, collapsed ? "1" : "0"); } catch (e) { /* сховище недоступне */ }
    });
  }
})();
