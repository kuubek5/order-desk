document.querySelectorAll(".login-glow").forEach((wrap) => {
  wrap.addEventListener("mousemove", (event) => {
    const rect = wrap.getBoundingClientRect();
    wrap.style.setProperty("--glow-x", `${event.clientX - rect.left}px`);
    wrap.style.setProperty("--glow-y", `${event.clientY - rect.top}px`);
  });
  wrap.addEventListener("mouseenter", () => wrap.classList.add("is-hovering"));
  wrap.addEventListener("mouseleave", () => wrap.classList.remove("is-hovering"));
});

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
