document.querySelectorAll(".copy-path-btn").forEach((button) => {
  button.addEventListener("click", async (event) => {
    event.preventDefault();
    const valueText = button.getAttribute("data-copy-value");

    // Try modern clipboard API first
    if (navigator.clipboard && navigator.clipboard.writeText) {
      try {
        await navigator.clipboard.writeText(valueText);
        showCopySuccess(button);
      } catch (err) {
        // Fallback to older execCommand if clipboard API fails
        fallbackCopy(valueText, button);
      }
    } else {
      // Fallback for environments without clipboard API
      fallbackCopy(valueText, button);
    }
  });
});

function showCopySuccess(button) {
  const iconCopy = button.querySelector(".icon-copy");
  const iconCheck = button.querySelector(".icon-check");

  // Swap icons: hide clipboard, show checkmark
  iconCopy.setAttribute("hidden", "");
  iconCheck.removeAttribute("hidden");

  // Add success state class for CSS animations
  button.classList.add("copied");

  // Revert after 1.2 seconds
  setTimeout(() => {
    iconCheck.setAttribute("hidden", "");
    iconCopy.removeAttribute("hidden");
    button.classList.remove("copied");
  }, 1200);
}

function fallbackCopy(text, button) {
  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.style.position = "fixed";
  textarea.style.opacity = "0";
  document.body.appendChild(textarea);

  try {
    textarea.select();
    const success = document.execCommand("copy");
    if (success) {
      showCopySuccess(button);
    }
  } catch (err) {
    // Silently fail—no alerts or error messages
  } finally {
    document.body.removeChild(textarea);
  }
}
