// Налаштування: перетягування файлу бекапа в зону відновлення.
//
// Клік і так працює через рідний <label> — це додає drag&drop і показ імені
// обраного файлу. Мовчить на сторінках без .dropzone.

// Drag-and-drop file zone (settings backup restore, .dropzone wrapping a hidden
// file input). Click already works via the native <label>; this adds drag/drop
// and shows the chosen filename. No-op on pages without a .dropzone.
(function () {
  const zone = document.getElementById("backup-dropzone");
  if (!zone) return;
  const input = zone.querySelector(".dropzone-input");
  const fileEl = zone.querySelector(".dropzone-file");
  const titleEl = zone.querySelector(".dropzone-title");
  const hintEl = zone.querySelector(".dropzone-hint");
  if (!input) return;

  function showFile(name) {
    if (!name) {
      zone.classList.remove("has-file");
      if (fileEl) { fileEl.hidden = true; fileEl.textContent = ""; }
      if (titleEl) titleEl.hidden = false;
      if (hintEl) hintEl.hidden = false;
      return;
    }
    zone.classList.add("has-file");
    if (titleEl) titleEl.hidden = true;
    if (hintEl) hintEl.hidden = true;
    if (fileEl) { fileEl.hidden = false; fileEl.textContent = name; }
  }

  input.addEventListener("change", () => {
    showFile(input.files && input.files[0] ? input.files[0].name : "");
  });

  ["dragenter", "dragover"].forEach((evt) =>
    zone.addEventListener(evt, (e) => {
      e.preventDefault();
      zone.classList.add("is-dragover");
    })
  );
  ["dragleave", "dragend"].forEach((evt) =>
    zone.addEventListener(evt, (e) => {
      // Ignore dragleave bubbling from children still inside the zone.
      if (evt === "dragleave" && zone.contains(e.relatedTarget)) return;
      zone.classList.remove("is-dragover");
    })
  );
  zone.addEventListener("drop", (e) => {
    e.preventDefault();
    zone.classList.remove("is-dragover");
    const files = e.dataTransfer && e.dataTransfer.files;
    if (!files || !files.length) return;
    input.files = files; // assigning a FileList is supported for drops
    input.dispatchEvent(new Event("change", { bubbles: true }));
  });
})();
