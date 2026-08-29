// Екран «Зміна»: скріншоти в записку — вставка з буфера, файл, перетягування.
//
// Три входи навмисно, бо це три різні звички: Ctrl+V після Win+Shift+S,
// готовий файл із Print Screen, і перетягування з Провідника. Усі три
// зводяться до одного `<input type="file" multiple>`, який форма відправляє
// звичайним multipart — без JS вона теж працює, просто без Ctrl+V.
(() => {
  const MAX_FILES = 4;

  function inputFor(zone) {
    return zone.querySelector("input[type=file]");
  }

  // FileList доступний лише для читання, тому набір збираємо через DataTransfer
  // і присвоюємо назад — єдиний спосіб долити файл до наявного вибору.
  function setFiles(input, files) {
    const bag = new DataTransfer();
    files.slice(0, MAX_FILES).forEach((file) => bag.items.add(file));
    input.files = bag.files;
    renderThumbs(input);
  }

  function currentFiles(input) {
    return Array.from(input.files || []);
  }

  function addFiles(input, incoming) {
    const images = Array.from(incoming).filter((f) => f && f.type.startsWith("image/"));
    if (!images.length) return false;
    setFiles(input, currentFiles(input).concat(images));
    return true;
  }

  function renderThumbs(input) {
    const zone = input.closest("[data-shift-drop]");
    const strip = zone && zone.querySelector("[data-shift-thumbs]");
    if (!strip) return;
    // Кожен createObjectURL треба звільнити, інакше вкладка, відкрита цілу
    // зміну, тримає пам'ять під кожен переглянутий скріншот.
    strip.querySelectorAll("img[src^='blob:']").forEach((img) => URL.revokeObjectURL(img.src));
    strip.innerHTML = "";
    currentFiles(input).forEach((file, index) => {
      const item = document.createElement("span");
      item.className = "sh-thumb";
      const img = document.createElement("img");
      img.src = URL.createObjectURL(file);
      img.alt = file.name;
      const drop = document.createElement("button");
      drop.type = "button";
      drop.className = "sh-thumb-x";
      drop.setAttribute("aria-label", "Прибрати скріншот");
      drop.textContent = "✕";
      drop.addEventListener("click", () => {
        setFiles(input, currentFiles(input).filter((_, i) => i !== index));
      });
      item.append(img, drop);
      strip.append(item);
    });
    strip.hidden = strip.children.length === 0;
  }

  document.addEventListener("paste", (event) => {
    const zone = event.target.closest && event.target.closest("[data-shift-drop]");
    if (!zone || !event.clipboardData) return;
    const input = inputFor(zone);
    if (input && addFiles(input, event.clipboardData.files)) {
      // Скасовуємо лише коли справді забрали картинку: інакше зламали б
      // звичайну вставку тексту в те саме поле.
      event.preventDefault();
    }
  });

  document.addEventListener("dragover", (event) => {
    if (event.target.closest && event.target.closest("[data-shift-drop]")) event.preventDefault();
  });

  document.addEventListener("drop", (event) => {
    const zone = event.target.closest && event.target.closest("[data-shift-drop]");
    if (!zone || !event.dataTransfer) return;
    const input = inputFor(zone);
    if (input && addFiles(input, event.dataTransfer.files)) event.preventDefault();
  });

  document.addEventListener("change", (event) => {
    const input = event.target;
    if (input.matches && input.matches("[data-shift-drop] input[type=file]")) renderThumbs(input);
  });

  // Файл, якого немає на диску (не відновився з бекапу — бекап не несе байтів),
  // має виглядати ТАК САМО, як прибраний через 6 місяців: один деградований
  // стан замість двох різних. Слухач на фазі захоплення, бо `error` на <img>
  // не спливає.
  document.addEventListener(
    "error",
    (event) => {
      const img = event.target;
      if (!img.matches || !img.matches("[data-shot]")) return;
      const shot = img.closest(".sh-shot");
      if (shot) shot.classList.add("is-gone");
    },
    true
  );
})();
