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
  const quotes = [
    "а коли буде пічка?",
    "коли буде слм?",
    "швидку закрили?",
    "де моя коронка бліна?",
  ];
  const EXIT_MS = 460; // a hair past the .dyn-item transition, before removal
  // Curated bright palette — each letter takes the next colour, wrapping round.
  const palette = [
    "#5eead4", // teal
    "#7dd3fc", // sky
    "#a5b4fc", // indigo
    "#c4b5fd", // violet
    "#f0abfc", // fuchsia
    "#fda4af", // rose
    "#fcd34d", // amber
    "#86efac", // green
  ];

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
