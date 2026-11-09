// Екран «Клієнти», режим «Майстер».
//
// Один обробник — і він потрібен: HTMX міняє лише праву картку, тож підсвітку
// обраного в лівому списку переносимо самі.

// Клієнти, режим «Майстер»: HTMX міняє лише праву картку, тож підсвітку
// обраного в лівому списку треба перенести самим. Без цього клас `on` лишався
// на попередньому клієнті — картка показувала одного, список підсвічував
// іншого, і на списку в кілька сотень рядків оператор губив своє місце.
// Робимо це на click, а не на htmx:afterSwap, щоб підсвітка стрибала одразу,
// не чекаючи відповіді сервера.
document.addEventListener("click", (event) => {
  const item = event.target.closest("[data-cl-item]");
  if (!item) return;
  document.querySelectorAll("[data-cl-item].on").forEach((el) => el.classList.remove("on"));
  item.classList.add("on");
});

// Картка клієнта: докази під фактом, підказка й перехрестя на карті «Коли пише».
document.addEventListener("click", (event) => {
  const open = event.target.closest("[data-cx-open]");
  if (open) {
    const fact = open.closest("[data-cx-fact]");
    open.setAttribute("aria-expanded", String(fact.classList.toggle("open")));
    return;
  }
  const cell = event.target.closest("[data-cx-cell]");
  if (cell) {
    const heat = cell.closest("[data-cx-heat]");
    const again = cell.classList.contains("on");
    heat.querySelectorAll(".cx-cell.on").forEach((el) => el.classList.remove("on"));
    const slot = document.getElementById("cx-slot");
    if (again) {
      if (slot) slot.innerHTML = "";
      return;
    }
    cell.classList.add("on");
  }
}, true);

(() => {
  let tip = null;
  const show = (text, x, y) => {
    if (!tip) {
      tip = document.createElement("div");
      tip.id = "cx-tip";
      tip.setAttribute("role", "status");
      document.body.append(tip);
    }
    const [title, rest] = text.split("|");
    tip.textContent = "";
    const b = document.createElement("b");
    b.textContent = title;
    const span = document.createElement("span");
    span.textContent = rest || "";
    tip.append(b, span);
    tip.classList.add("on");
    const width = tip.offsetWidth;
    const height = tip.offsetHeight;
    tip.style.left = `${Math.max(6, Math.min(window.innerWidth - width - 6, x - width / 2))}px`;
    tip.style.top = `${y - height - 10 < 4 ? y + 18 : y - height - 10}px`;
  };
  document.addEventListener("mousemove", (event) => {
    const target = event.target;
    if (!target.closest) return;
    document.querySelectorAll(".cx-cell.lit").forEach((el) => el.classList.remove("lit"));
    const cell = target.closest("[data-cx-cell]");
    if (cell) {
      const [y, x] = cell.dataset.cxCell.split(",");
      cell.closest("[data-cx-heat]").querySelectorAll("[data-cx-cell]").forEach((el) => {
        const [yy, xx] = el.dataset.cxCell.split(",");
        if (yy === y || xx === x) el.classList.add("lit");
      });
    }
    const hint = target.closest("#client-pane [data-tip]");
    if (hint) show(hint.dataset.tip, event.clientX, event.clientY);
    else if (tip) tip.classList.remove("on");
  });
})();

// Циферблат «Коли пише» (власник 09.11.26): стрілка «зараз», «наступний звичний
// лист», підсвітка кільця дня, Enter по сектору. Картка приходить через HTMX,
// тож усе вішається на htmx:load нового шматка, а не раз на сторінку.
(() => {
  const pad = (n) => String(n).padStart(2, "0");
  const initDial = (root) => {
    root.querySelectorAll("[data-cx-heat]").forEach((dial) => {
      if (dial.dataset.ready) return;
      dial.dataset.ready = "1";
      const now = new Date();
      const hand = dial.querySelector("[data-cx-hand]");
      if (hand) {
        const box = dial.querySelector("svg").viewBox.baseVal;
        hand.style.transformOrigin = `${box.width / 2}px ${box.height / 2}px`;
        hand.style.rotate = `${((now.getHours() + now.getMinutes() / 60) / 24) * 360}deg`;
        hand.removeAttribute("hidden"); // SVG-елемент не має властивості .hidden
      }
      const next = dial.querySelector("[data-cx-next]");
      if (next) {
        const day = (now.getDay() + 6) % 7;
        const hour = now.getHours();
        const has = (h) => Number(dial.querySelector(`[data-cx-cell="${day},${h}"]`)?.dataset.n || 0) > 0;
        const ahead = Array.from({ length: 24 - hour }, (_, i) => hour + i).find(has);
        next.textContent = "";
        if (ahead === hour) {
          next.append("Зазвичай пише ");
          const b = document.createElement("b");
          b.textContent = "саме зараз";
          next.append(b);
        } else if (ahead !== undefined) {
          next.append("Сьогодні зазвичай ~");
          const b = document.createElement("b");
          b.textContent = `${pad(ahead)}:00`;
          next.append(b);
        } else {
          next.textContent = "Сьогодні вже зазвичай не пише";
        }
      }
    });
  };
  document.addEventListener("htmx:load", (event) => initDial(event.detail.elt));
  if (document.readyState !== "loading") initDial(document);
  else document.addEventListener("DOMContentLoaded", () => initDial(document));

  document.addEventListener("click", (event) => {
    const btn = event.target.closest?.("[data-cx-day]");
    if (!btn) return;
    const row = btn.closest(".cx-dialrow");
    const dial = row?.querySelector("[data-cx-heat]");
    if (!dial) return;
    const on = btn.getAttribute("aria-pressed") !== "true";
    row.querySelectorAll("[data-cx-day]").forEach((el) => el.setAttribute("aria-pressed", "false"));
    dial.querySelectorAll("[data-cx-cell]").forEach((el) => {
      el.classList.toggle("hit", on && el.dataset.cxCell.split(",")[0] === btn.dataset.cxDay);
    });
    dial.classList.toggle("focusday", on);
    if (on) btn.setAttribute("aria-pressed", "true");
  });

  document.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" && event.key !== " ") return;
    const cell = event.target.closest?.("[data-cx-heat] .cx-cell.has");
    if (!cell) return;
    event.preventDefault();
    cell.dispatchEvent(new MouseEvent("click", { bubbles: true }));
  });
})();
