document.querySelectorAll(".login-glow").forEach((wrap) => {
  wrap.addEventListener("mousemove", (event) => {
    const rect = wrap.getBoundingClientRect();
    wrap.style.setProperty("--glow-x", `${event.clientX - rect.left}px`);
    wrap.style.setProperty("--glow-y", `${event.clientY - rect.top}px`);
  });
  wrap.addEventListener("mouseenter", () => wrap.classList.add("is-hovering"));
  wrap.addEventListener("mouseleave", () => wrap.classList.remove("is-hovering"));
});

// Dynamic text: cycling shop-floor quotes above the login card. Each swap
// stacks a fresh line over the current one — the new line rises from below and
// fades in while the old one flies up and fades out (both animate together, the
// KokonutUI dynamic-text feel). Loops. No-op if the container isn't present.
(function () {
  const box = document.getElementById("login-quotes");
  if (!box) return;
  // Третя нескінченна петля на екрані входу (поряд із диском і пилом), і
  // єдина, що міняє ТЕКСТ кожні 2.4 с. CSS гасив лише transform, тож для
  // reduced-motion рядок усе одно мигтів. Показуємо одну репліку й виходимо.
  const stillPlease = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const quotes = [
    "а коли буде пічка?",
    "коли буде слм?",
    "швидку закрили?",
    "де моя коронка бліна?",
  ];
  const EXIT_MS = 460; // a hair past the .dyn-item transition, before removal
  // Curated bright palette — each letter takes the next colour, wrapping round.
  // Кольори з токенів теми, а не сирі hex: вісім захардкоджених відтінків
  // лишались бірюзово-фіолетовими в Amber Forge, тобто екран входу єдиний
  // не підхоплював тему оператора.
  const palette = ["var(--accent-c)", "var(--accent-b)", "var(--accent-e)", "var(--ink-2)"];

  function makeItem(text) {
    const item = document.createElement("span");
    item.className = "dyn-item";
    const dot = document.createElement("span");
    dot.className = "dyn-dot";
    dot.setAttribute("aria-hidden", "true");
    item.appendChild(dot);
    // Letters go in their own inline wrapper — NOT straight into .dyn-item,
    // which is a flex row (its gap would fall between every letter and flex
    // would swallow the word spaces). Inside .dyn-words normal text flow keeps
    // the spaces between words.
    const words = document.createElement("span");
    words.className = "dyn-words";
    let c = 0;
    for (const ch of text) {
      if (ch === " ") {
        words.appendChild(document.createTextNode(" ")); // hard space
        continue;
      }
      const s = document.createElement("span");
      s.className = "ltr";
      s.textContent = ch;
      s.style.color = palette[c % palette.length];
      words.appendChild(s);
      c++;
    }
    item.appendChild(words);
    return item;
  }

  let i = 0;
  let current = makeItem(quotes[0]);
  box.appendChild(current); // first line sits at rest, no entrance

  if (stillPlease) return; // одна репліка, без каруселі

  setInterval(() => {
    i = (i + 1) % quotes.length;

    const incoming = makeItem(quotes[i]);
    incoming.classList.add("enter"); // start below, invisible
    box.appendChild(incoming);
    void incoming.offsetWidth; // reflow so the enter state paints first
    incoming.classList.remove("enter"); // …then animate it to rest

    const outgoing = current;
    outgoing.classList.add("exit"); // fly up + fade
    setTimeout(() => outgoing.remove(), EXIT_MS);

    current = incoming;
  }, 2400);
})();

document.querySelectorAll(".login-toggle-password").forEach((button) => {
  button.addEventListener("click", () => {
    const input = button.parentElement.querySelector(".login-input");
    const isHidden = input.type === "password";
    input.type = isHidden ? "text" : "password";
    button.setAttribute("aria-label", isHidden ? "Приховати пароль" : "Показати пароль");
    const eyeIcon = button.querySelector(".icon-eye");
    const eyeOffIcon = button.querySelector(".icon-eye-off");
    eyeIcon.toggleAttribute("hidden", isHidden);
    eyeOffIcon.toggleAttribute("hidden", !isHidden);
  });
});

/* ═══ Реєстрація оператора: Alt+Enter ═══════════════════════════════════════
   Рішення власника 08.09.26: на звичайному завантаженні реєстрації немає,
   Alt+Enter показує кнопку, кнопка розкриває форму.

   РОЗМІТКУ БУДУЄМО ТУТ, а не в шаблоні. Причина не в зручності: поки форми
   немає в DOM, її немає й для клавіші Tab, і для читача екрана — тобто
   «прихованість» справжня, а не косметична. Атрибут hidden дав би те саме для
   ока, але лишив би форму у вихідному коді сторінки.

   Що це НЕ є: захистом. Комбінацію знає той, хто знає, і після виходу в
   мережу сторінку входу бачитиме кожен у цеху. Власникові це показано, він
   обрав зручність свідомо (ROADMAP хід 17, RESILIENCE_PLAN). Захист живе на
   сервері: роль завжди «оператор», обмежувач спроб, слід у журналі дій.

   Вигляд — «панель верстата», поява — «спікання». Обидва обрані з макетів. */
(function () {
  "use strict";

  var slot = document.getElementById("reg-slot");
  if (!slot) return;

  var REDUCED = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var open = false;

  // Шкала спікання: та сама послідовність кольорів, що на табло печей —
  // від темного жару через помаранчевий до бурштину, потім охолодження.
  // Тримаємо в JS, а не в @keyframes: так одна перевірка вимикає всю анімацію
  // для того, хто просив менше руху.
  var HEAT = [
    ["#3d1a0b", "transparent", "0 0 14px -6px rgba(255,80,40,.5)"],
    ["#7a2a10", "transparent", "0 0 22px -4px rgba(255,107,82,.75)"],
    ["#c25a18", "rgba(255,216,148,.35)", "0 0 30px -2px rgba(255,138,61,.9)"],
    ["#ffb454", "#ffd894", "0 0 26px -6px rgba(255,180,84,.85)"],
    ["#a8752b", "#ffd894", "0 0 14px -10px rgba(255,180,84,.5)"]
  ];

  function field(name, label, type, placeholder, value) {
    return '<div class="reg-field">' +
      '<label for="reg-' + name + '">' + label + '</label>' +
      '<div class="reg-lcd">' +
      '<input type="' + type + '" id="reg-' + name + '" name="' + name + '"' +
      ' placeholder="' + placeholder + '" value="' + (value || "") + '"' +
      ' autocomplete="' + (type === "password" ? "new-password" : name === "username" ? "username" : "name") + '"' +
      ' required>' +
      '</div></div>';
  }

  function buildPanel() {
    var panel = document.createElement("div");
    panel.className = "reg-panel";
    var err = slot.dataset.error || "";
    panel.innerHTML =
      '<div class="reg-bar">' +
        '<span class="reg-dot" aria-hidden="true"></span>Реєстрація<b>готово</b>' +
        '<button type="button" class="reg-close" aria-label="Сховати реєстрацію">&#10005;</button>' +
      '</div>' +
      '<div class="reg-body">' +
        (err ? '<p class="reg-error" role="alert">' + err + '</p>' : "") +
        '<form method="post" action="/register">' +
          field("full_name", "Ваше імʼя", "text", "Іван Петренко", slot.dataset.fullName) +
          field("username", "Логін", "text", "імʼя.прізвище", slot.dataset.username) +
          field("password", "Пароль", "password", "••••••••", "") +
          field("password_confirmation", "Пароль ще раз", "password", "••••••••", "") +
          '<button type="submit" class="reg-submit">Зареєструватись</button>' +
        '</form>' +
      '</div>';
    panel.querySelector(".reg-close").addEventListener("click", collapse);
    return panel;
  }

  function showPanel() {
    if (slot.querySelector(".reg-panel")) return;
    // Кнопка ховається: у панелі є власна шапка «Реєстрація», і два заголовки
    // поспіль читались би як два різні блоки. Кнопка зробила свою роботу.
    var reveal = slot.querySelector(".reg-reveal");
    if (reveal) reveal.remove();
    var panel = buildPanel();
    // Проступає крізь теплове марево — та сама ідіома, що й у появі кнопки.
    if (!REDUCED) panel.style.filter = "blur(7px) saturate(1.6)";
    slot.appendChild(panel);
    requestAnimationFrame(function () {
      panel.classList.add("is-on");
      panel.style.filter = "none";
      var first = panel.querySelector("input");
      if (first) first.focus();
    });
  }

  function expand(instant) {
    if (open) return;
    open = true;
    slot.innerHTML = "";

    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "reg-reveal";
    btn.textContent = "Реєстрація";
    slot.appendChild(btn);
    btn.addEventListener("click", showPanel);

    if (REDUCED || instant) {
      btn.style.background = "linear-gradient(180deg,#a8752b,#1d0f06)";
      btn.style.borderColor = "#a8752b";
      btn.style.color = "#ffd894";
      if (instant) showPanel();
      return;
    }

    var i = 0;
    (function heat() {
      if (i >= HEAT.length) { btn.focus(); return; }
      var s = HEAT[i++];
      btn.style.transition =
        "background .32s ease, color .32s ease, box-shadow .32s ease, border-color .32s ease";
      btn.style.background = "linear-gradient(180deg," + s[0] + ",#1d0f06)";
      btn.style.borderColor = s[0];
      btn.style.color = s[1];
      btn.style.boxShadow = s[2];
      setTimeout(heat, 290);
    })();
  }

  function collapse() {
    open = false;
    slot.innerHTML = "";
    var login = document.getElementById("username");
    if (login) login.focus();
  }

  document.addEventListener("keydown", function (e) {
    if (e.altKey && e.key === "Enter") {
      e.preventDefault();
      if (open) collapse(); else expand(false);
    }
  });

  // Сервер перемалював сторінку через помилку у формі — розгортаємо ОДРАЗУ,
  // без анімації: людина вже тут, і повторний показ жару читався б як
  // «щось почалось заново», а не «виправте поле».
  if (slot.dataset.open) expand(true);
})();
