document.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-copy]");
  if (!button) return;

  const value = button.dataset.copy || "";
  if (!value) return;

  const originalTitle = button.title;
  try {
    await navigator.clipboard.writeText(value);
  } catch (_error) {
    const input = document.createElement("textarea");
    input.value = value;
    input.setAttribute("readonly", "");
    input.style.position = "fixed";
    input.style.opacity = "0";
    document.body.appendChild(input);
    input.select();
    document.execCommand("copy");
    input.remove();
  }

  button.title = "Скопійовано";
  button.classList.add("copy-success");
  window.setTimeout(() => {
    button.title = originalTitle;
    button.classList.remove("copy-success");
  }, 1400);
});

// Collapsible "Нові з пошти" block on the queue dashboard (CLAUDE.md section
// 8): starts collapsed to a single summary row, expands on click. No-op on
// every other page — the toggle button only exists on the queue screen.
document.addEventListener("click", (event) => {
  const toggle = event.target.closest("[data-pending-mail-toggle]");
  if (!toggle) return;

  const section = toggle.closest(".pending-mail");
  if (!section) return;

  const collapsed = section.classList.toggle("is-collapsed");
  toggle.setAttribute("aria-expanded", String(!collapsed));
});
