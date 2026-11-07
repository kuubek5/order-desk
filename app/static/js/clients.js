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
