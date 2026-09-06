/*
 * Click-stable STL preview panel for the queue's folder icons.
 *
 * History: this used to be a fragile HOVER popup — the preview vanished the
 * instant the cursor left the folder icon, before it could reach the popup
 * across the gap (classic hover-bridge problem), which made switching between
 * a folder's multiple .stl files practically impossible. It is now a CLICK-
 * stable floating panel, the same interaction model as the mail triage
 * gallery (app/static/js/stl-gallery.js, already approved by the user):
 *
 *   - Clicking a folder icon ([data-stl-preview-token] that is NOT the
 *     job-code copy-button) opens a stable panel positioned next to the row.
 *   - The panel does NOT disappear on mouseout. It lists the folder's .stl
 *     files (clickable names) plus one large 3D canvas; clicking a name swaps
 *     the model in the single shared WebGL context.
 *   - Closes on the ✕ button, Esc, or a click outside the panel.
 *
 * The job-code copy-button also carries a token, but its primary action is
 * "copy path-ID for Sum3D" (CLAUDE.md screen 1) — a very frequent click — so
 * it is deliberately excluded from opening this (heavy, WebGL) panel via the
 * `:not([data-copy])` selector. The folder icon (.folder-link <a>) is the
 * preview affordance now; that anchor's default navigation is suppressed while
 * the panel is open, and an "Відкрити папку" button inside the panel preserves
 * the ability to open the real folder in Explorer.
 *
 * Design notes carried over from the old hover version:
 *  - The token is opaque; this script never inspects or builds paths from it,
 *    it only round-trips it back to /stl-preview/{token}[/{filename}] (list +
 *    bytes routes, path safety enforced in app/stl_preview.py).
 *  - One reusable WebGL context / panel for the whole page (browsers cap
 *    concurrent WebGL contexts, and the queue can have dozens of folder icons).
 *  - Geometries are cached per token+filename so re-selecting a file is
 *    instant; a pending fetch is abortable and abandoned if the user switches
 *    files or closes the panel first.
 *  - Respects prefers-reduced-motion: a static render instead of the auto-
 *    rotate RAF loop.
 */
(function () {
  "use strict";

  // Спільне ядро (stl-render-core.js) тримає все, що однакове з галереєю
  // тріажу: WebGL-контекст, парсинг STL, центрування моделі, колір із
  // токена, звільнення контексту. Немає ядра або three.js — панелі немає.
  const Core = window.StlRenderCore;
  if (!Core || !Core.available()) {
    return;
  }

  const REDUCED_MOTION = Core.reducedMotion();

  // Швидкість авто-обертання запам'ятовується між сесіями (прохання оператора
  // 28.08.26): виставив слайдером — так і лишається наступного разу, поки сам
  // не зміниш. Правий клік «стоп щоб роздивитись» СЮДИ не пише — це разова
  // заморозка, а збережений вибір повертається при новому відкритті.
  const SPEED_STORAGE_KEY = "stl-preview-spin-speed";

  // Розмір панелі теж запам'ятовується, і дефолт тут — РОЗГОРНУТА (аудит
  // 05.09.26, UX 1.5). STL-прев'ю — головний інструмент звірки на видачі
  // (CLAUDE.md §2, §9.4): оператор відкриває його на кожну коронку і щоразу
  // мусив тиснути «розгорнути». На 300×240 сусідні анатомії не розрізняються,
  // тож маленька панель — це і зайвий клік, і ризик видати не ту роботу.
  const MAX_STORAGE_KEY = "stl-preview-max";

  // Дефолт живе САМЕ ТУТ, а не в ядрі: панель видачі без збереженого
  // вибору відкривається РОЗГОРНУТОЮ, галерея тріажу — вбудованою.
  function loadMaxPreference() {
    return Core.readBool(MAX_STORAGE_KEY, true);
  }

  function saveMaxPreference(on) {
    Core.writeBool(MAX_STORAGE_KEY, on);
  }

  function loadSavedSpeed() {
    return Core.readNumber(SPEED_STORAGE_KEY, 0, 3); // у межах слайдера
  }

  function saveSpeed(v) {
    Core.writeNumber(SPEED_STORAGE_KEY, v);
  }

  // Збережене значення має пріоритет над дефолтом; якщо нічого не збережено —
  // reduced-motion лишає 0, інакше звичний 1.
  const SAVED_SPEED = loadSavedSpeed();

  // Кеші геометрій/списків файлів — LRU з капом. Панель живе всю зміну, а
  // оператор на видачі відкриває сотні тек: без капу heap ріс безмежно
  // (ревʼю 07.09.26). Map зберігає порядок вставки — найстаріший ключ перший.
  const GEOMETRY_CACHE_MAX = 40;
  const FILELIST_CACHE_MAX = 200;
  function cacheSet(map, key, value, max) {
    if (map.has(key)) map.delete(key);
    map.set(key, value);
    while (map.size > max) {
      const oldest = map.keys().next().value;
      const gone = map.get(oldest);
      map.delete(oldest);
      if (gone && typeof gone.dispose === "function") gone.dispose();
    }
  }

  const state = {
    token: null,
    triggerEl: null,
    folderUri: null,
    files: [],
    activeIndex: -1,
    controller: null,
    rafId: null,
    spinSpeed: SAVED_SPEED !== null ? SAVED_SPEED : (REDUCED_MOTION ? 0 : 1), // збережений вибір або дефолт
    dragging: false, // права кнопка затиснута — ручне обертання
    dragLastX: 0,
    dragLastY: 0,
    speedEl: null,
    fileListCache: new Map(), // token -> string[]  (LRU, див. cacheSet)
    geometryCache: new Map(), // "token filename" -> BufferGeometry (raw)  (LRU)
    renderer: null,
    scene: null,
    camera: null,
    mesh: null,
    panelEl: null,
    canvasEl: null,
    statusEl: null,
    filesEl: null,
    folderBtnEl: null,
    open: false,
  };

  function geoKey(token, filename) {
    return `${token} ${filename}`;
  }

  function ensurePanel() {
    if (state.panelEl) return;

    const panel = document.createElement("div");
    panel.className = "stl-panel";
    panel.setAttribute("role", "dialog");
    panel.setAttribute("aria-label", "Перегляд STL");
    panel.hidden = true;

    const head = document.createElement("div");
    head.className = "stl-panel-head";
    const title = document.createElement("span");
    title.className = "stl-panel-title mono";
    title.textContent = "STL прев'ю";
    head.appendChild(title);
    // Розгортання на весь екран. CLAUDE.md §2 і §9.4 називають STL-прев'ю
    // ГОЛОВНИМ інструментом звірки — оператор порівнює форму коронки з лотка
    // з моделлю, «зазвичай у повноекранному режимі». На 300×240 сусідні
    // анатомії не розрізняються, і це прямий шлях видати не ту роботу.
    const maxBtn = document.createElement("button");
    maxBtn.type = "button";
    maxBtn.className = "stl-panel-max";
    maxBtn.setAttribute("aria-label", "Розгорнути на весь екран");
    maxBtn.title = "Розгорнути на весь екран";
    maxBtn.innerHTML =
      '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/></svg>';
    maxBtn.addEventListener("click", () => {
      const on = state.panelEl.classList.toggle("is-max");
      syncMaxButton(on);
      // Свідомий вибір кнопкою — запам'ятовуємо. Вихід через Esc НЕ пишеться:
      // це разова дія «згорнути зараз», а не зміна звички.
      saveMaxPreference(on);
      resizeRenderer();
    });
    state.maxBtnEl = maxBtn;
    head.appendChild(maxBtn);

    const closeBtn = document.createElement("button");
    closeBtn.type = "button";
    closeBtn.className = "stl-panel-close";
    closeBtn.setAttribute("aria-label", "Закрити");
    closeBtn.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6 6 18"/></svg>';
    closeBtn.addEventListener("click", closePanel);
    head.appendChild(closeBtn);
    panel.appendChild(head);

    const stage = document.createElement("div");
    stage.className = "stl-panel-stage";
    const canvas = document.createElement("canvas");
    canvas.className = "stl-panel-canvas";
    stage.appendChild(canvas);
    attachManualRotation(canvas);
    const status = document.createElement("div");
    status.className = "stl-panel-status";
    stage.appendChild(status);
    panel.appendChild(stage);

    // Rotation-speed slider: 0 (frozen) … 3× the default spin. Lets the
    // operator slow a busy model down to inspect it, or spin it up.
    const speedRow = document.createElement("div");
    speedRow.className = "stl-panel-speed";
    const speedIcon = document.createElement("span");
    speedIcon.className = "stl-speed-icon";
    speedIcon.setAttribute("aria-hidden", "true");
    speedIcon.innerHTML =
      '<svg viewBox="0 0 24 24"><path d="M21 12a9 9 0 1 1-9-9"/><path d="M21 3v6h-6"/></svg>';
    speedRow.appendChild(speedIcon);
    const speedInput = document.createElement("input");
    speedInput.type = "range";
    speedInput.className = "stl-speed-range";
    speedInput.min = "0";
    speedInput.max = "3";
    speedInput.step = "0.1";
    speedInput.value = String(state.spinSpeed);
    speedInput.setAttribute("aria-label", "Швидкість обертання");
    speedInput.addEventListener("input", () => {
      state.spinSpeed = Number(speedInput.value) || 0;
      saveSpeed(state.spinSpeed); // тільки слайдер запам'ятовується, не правий клік
      // Nudge the loop: if it self-stopped at speed 0, resume it; renderOnce
      // keeps the frozen model visible when the operator drags back to 0.
      if (state.open && state.spinSpeed > 0 && state.rafId === null) {
        startRenderLoop();
      } else if (state.spinSpeed === 0) {
        renderOnce();
      }
    });
    speedRow.appendChild(speedInput);
    panel.appendChild(speedRow);
    state.speedEl = speedInput;

    const files = document.createElement("div");
    files.className = "stl-panel-files";
    files.setAttribute("role", "list");
    files.setAttribute("aria-label", "STL-файли папки");
    files.addEventListener("click", (event) => {
      const btn = event.target.closest(".stl-panel-file");
      if (!btn) return;
      const index = Number(btn.dataset.index);
      if (index === state.activeIndex) return;
      selectFile(index);
    });
    panel.appendChild(files);

    const foot = document.createElement("div");
    foot.className = "stl-panel-foot";
    const folderBtn = document.createElement("button");
    folderBtn.type = "button";
    folderBtn.className = "stl-panel-folder";
    folderBtn.hidden = true;
    folderBtn.innerHTML =
      '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 7h7l2 2h9v10H3z"/><path d="M3 7V5h7l2 2"/></svg><span>Відкрити папку</span>';
    // A browser silently blocks a file:// link opened from an http page, so
    // open the real folder via the authenticated loopback-only server route
    // instead of navigating. Re-derives the folder from the opaque token.
    folderBtn.addEventListener("click", () => {
      if (!state.token) return;
      const original = folderBtn.querySelector("span").textContent;
      const body = new URLSearchParams({ token: state.token });
      fetch("/open-folder", { method: "POST", body, credentials: "same-origin" })
        .then((response) => {
          if (!response.ok) throw new Error("open-failed");
        })
        .catch(() => {
          folderBtn.querySelector("span").textContent = "Не вдалося відкрити";
          window.setTimeout(() => {
            folderBtn.querySelector("span").textContent = original;
          }, 2000);
        });
    });
    foot.appendChild(folderBtn);
    panel.appendChild(foot);

    document.body.appendChild(panel);

    state.panelEl = panel;
    state.canvasEl = canvas;
    state.statusEl = status;
    state.filesEl = files;
    state.folderBtnEl = folderBtn;
  }

  function ensureRenderer() {
    if (state.renderer) return;
    ensurePanel();
    // Ядро дописує canvas/renderer/scene/camera прямо в state, тож решта
    // файлу працює зі звичними state.renderer / state.camera / state.mesh.
    Core.createView(state, state.canvasEl);
  }

  function resizeRenderer() {
    Core.resizeView(state);
  }

  function setStatus(text) {
    ensurePanel();
    Core.applyStatus(state.statusEl, state.canvasEl, text);
  }

  function clearMesh() {
    Core.clearMesh(state);
  }

  function renderOnce() {
    Core.renderOnce(state);
  }

  function showGeometry(geometry) {
    ensureRenderer();
    // Нормалі, центрування, масштаб і камера — у ядрі (спільне з галереєю).
    // Камера при цьому вертається в дефолтну позицію — нова модель
    // має починатись із зрозумілого ракурсу, а не з чужого зуму.
    Core.showGeometry(state, geometry);
    setStatus(null);
    startRenderLoop();
  }

  // Ручне обертання правою кнопкою (прохання оператора 28.08.26). Клік правою
  // одразу глушить авто-обертання — щоб звіряти форму коронки зі STL, модель
  // має стояти рівно там, де оператор її лишив, а не крутитись під рукою.
  // Утримання правої + рух — крутить модель у двох осях. Ліве перетягування
  // теж крутить (звична дія), тому ловимо будь-яку кнопку.
  const DRAG_SENS = 0.01; // радіан на піксель
  const ZOOM_STEP = 1.1;  // множник відстані камери на одну «зубчик» колеса
  const ZOOM_MIN = 1.5;   // ближче не пускаємо — модель не влітає в екран
  const ZOOM_MAX = 8.0;   // далі не пускаємо — не губиться крапкою
  const WORLD_UP = new THREE.Vector3(0, 1, 0);
  const WORLD_RIGHT = new THREE.Vector3(1, 0, 0);

  function freezeSpin() {
    state.spinSpeed = 0;
    if (state.speedEl) state.speedEl.value = "0"; // слайдер показує реальний стан
    stopRenderLoop();
    renderOnce();
  }

  function attachManualRotation(canvas) {
    // Права кнопка не має відкривати системне меню поверх моделі.
    canvas.addEventListener("contextmenu", (event) => event.preventDefault());

    // Зум колесом: наближає/віддаляє камеру вздовж її погляду. Множимо
    // позицію камери на коефіцієнт (напрямок зберігається, міняється лише
    // відстань), тримаємо в межах, щоб модель не влетіла в екран і не зникла.
    // passive:false — щоб перехопити прокрутку сторінки під моделлю.
    canvas.addEventListener("wheel", (event) => {
      if (!state.camera) return;
      event.preventDefault();
      const factor = event.deltaY > 0 ? ZOOM_STEP : 1 / ZOOM_STEP;
      const pos = state.camera.position;
      const dist = pos.length() * factor;
      if (dist >= ZOOM_MIN && dist <= ZOOM_MAX) {
        pos.multiplyScalar(factor);
        state.camera.lookAt(0, 0, 0);
        renderOnce();
      }
    }, { passive: false });

    canvas.addEventListener("pointerdown", (event) => {
      if (!state.mesh) return;
      // Права (2) або ліва (0) кнопка. Права ще й глушить авто-спін.
      if (event.button !== 0 && event.button !== 2) return;
      freezeSpin();
      state.dragging = true;
      state.dragLastX = event.clientX;
      state.dragLastY = event.clientY;
      try { canvas.setPointerCapture(event.pointerId); } catch (_) { /* ok */ }
      event.preventDefault();
    });

    canvas.addEventListener("pointermove", (event) => {
      if (!state.dragging || !state.mesh) return;
      const dx = event.clientX - state.dragLastX;
      const dy = event.clientY - state.dragLastY;
      state.dragLastX = event.clientX;
      state.dragLastY = event.clientY;
      // Обертаємо навколо СВІТОВИХ осей (трекбол), а не додаємо кути Ейлера
      // на модель: інакше після першого повороту осі «замикаються» (gimbal
      // lock) і здається, що крутити можна не в усі боки. rotateOnWorldAxis
      // множить у світовому просторі, тож рух миші завжди означає те саме,
      // незалежно від поточного положення моделі.
      // Горизонталь — навколо світової вертикалі, вертикаль — навколо
      // світової горизонталі (правої осі екрана). Знак мінус — grab-and-drag:
      // тягнеш праворуч, видима грань іде за курсором.
      // Обидві осі — плюс (за проханням оператора): тягнеш вліво — модель
      // крутиться вліво, вправо — вправо, вгору/вниз відповідно; по діагоналі
      // обидві осі складаються самі, тож кути працюють без окремого коду.
      state.mesh.rotateOnWorldAxis(WORLD_UP, dx * DRAG_SENS);
      state.mesh.rotateOnWorldAxis(WORLD_RIGHT, dy * DRAG_SENS);
      renderOnce();
    });

    function endDrag(event) {
      if (!state.dragging) return;
      state.dragging = false;
      try { canvas.releasePointerCapture(event.pointerId); } catch (_) { /* ok */ }
    }
    canvas.addEventListener("pointerup", endDrag);
    canvas.addEventListener("pointercancel", endDrag);
    canvas.addEventListener("pointerleave", endDrag);
  }

  function startRenderLoop() {
    stopRenderLoop();
    // Frozen (slider at 0, or reduced-motion default): draw one still frame and
    // don't burn a RAF loop. The slider's input handler restarts the loop when
    // the operator drags the speed back above 0.
    if (state.spinSpeed <= 0) {
      renderOnce();
      return;
    }
    function tick() {
      if (!state.open || state.spinSpeed <= 0) {
        stopRenderLoop();
        renderOnce();
        return;
      }
      Core.spinMesh(state.mesh, state.spinSpeed);
      renderOnce();
      state.rafId = window.requestAnimationFrame(tick);
    }
    state.rafId = window.requestAnimationFrame(tick);
  }

  function stopRenderLoop() {
    if (state.rafId !== null) {
      window.cancelAnimationFrame(state.rafId);
      state.rafId = null;
    }
  }

  // Розмітка списку файлів спільна з галереєю; різниться лише клас кнопки.
  const FILE_CLASS = "stl-panel-file";

  function renderFileList() {
    ensurePanel();
    Core.renderFileList(state.filesEl, state.files, state.activeIndex, FILE_CLASS);
  }

  function updateActiveFile(index) {
    Core.updateActiveFile(state.filesEl, FILE_CLASS, index);
  }

  function selectFile(index) {
    if (index < 0 || index >= state.files.length) return;
    const token = state.token;
    state.activeIndex = index;
    updateActiveFile(index);

    const filename = state.files[index];
    const cached = state.geometryCache.get(geoKey(token, filename));
    if (cached) {
      showGeometry(cached.clone());
      return;
    }

    ensureRenderer();
    setStatus("Завантаження…");

    if (state.controller) state.controller.abort();
    const controller = new AbortController();
    state.controller = controller;

    Core.fetchGeometry(token, filename, controller.signal)
      .then((geometry) => {
        if (controller.signal.aborted) return;
        cacheSet(state.geometryCache, geoKey(token, filename), geometry, GEOMETRY_CACHE_MAX);
        if (state.token !== token || state.activeIndex !== index) return;
        showGeometry(geometry.clone());
      })
      .catch(() => {
        if (controller.signal.aborted) return;
        if (state.token === token && state.activeIndex === index) {
          setStatus("Не вдалося завантажити прев'ю");
        }
      });
  }

  function startWithFiles(token, files) {
    if (state.token !== token) return;
    state.files = files;
    if (files.length === 0) {
      state.activeIndex = -1;
      renderFileList();
      setStatus("Немає STL у папці");
      return;
    }
    renderFileList();
    selectFile(0);
  }

  function positionPanel(target) {
    ensurePanel();
    const panel = state.panelEl;
    // Measure with the panel laid out but off-screen to avoid a flash.
    panel.style.visibility = "hidden";
    panel.hidden = false;
    const pw = panel.offsetWidth;
    const ph = panel.offsetHeight;
    panel.hidden = true;
    panel.style.visibility = "";

    const rect = target.getBoundingClientRect();
    const margin = 10;
    let left = rect.right + margin;
    if (left + pw > window.innerWidth - margin) {
      left = rect.left - pw - margin;
    }
    left = Math.max(margin, Math.min(left, window.innerWidth - pw - margin));

    let top = rect.top + rect.height / 2 - ph / 2;
    top = Math.max(margin, Math.min(top, window.innerHeight - ph - margin));

    panel.style.left = `${left}px`;
    panel.style.top = `${top}px`;
  }

  function syncMaxButton(on) {
    if (!state.maxBtnEl) return;
    state.maxBtnEl.setAttribute("aria-label", on ? "Згорнути" : "Розгорнути на весь екран");
    state.maxBtnEl.title = state.maxBtnEl.getAttribute("aria-label");
  }

  function openPanel(triggerEl, token) {
    ensurePanel();

    const folderUri =
      (triggerEl.tagName === "A" && triggerEl.getAttribute("href")) ||
      triggerEl.dataset.folderUri ||
      null;

    // Re-clicking the same trigger toggles the panel shut.
    if (state.open && state.token === token && state.triggerEl === triggerEl) {
      closePanel();
      return;
    }

    if (state.controller) {
      state.controller.abort();
      state.controller = null;
    }

    state.token = token;
    state.triggerEl = triggerEl;
    state.folderUri = folderUri;
    state.files = [];
    state.activeIndex = -1;

    if (state.folderBtnEl) {
      if (folderUri) {
        state.folderBtnEl.href = folderUri;
        state.folderBtnEl.hidden = false;
      } else {
        state.folderBtnEl.hidden = true;
        state.folderBtnEl.removeAttribute("href");
      }
    }

    state.filesEl.innerHTML = "";
    // Розмір — такий, яким оператор лишив його минулого разу (дефолт: на весь
    // екран). Ставимо ДО positionPanel: у розгорнутому стані панель займає
    // екран і рахувати позицію біля рядка не треба.
    const wantMax = loadMaxPreference();
    state.panelEl.classList.toggle("is-max", wantMax);
    syncMaxButton(wantMax);
    positionPanel(triggerEl);
    state.panelEl.hidden = false;
    state.open = true;
    // Force a reflow so the opacity/transform transition runs from the just-
    // unhidden state, then flip the class synchronously. (Deliberately not a
    // requestAnimationFrame: rAF is throttled when the tab isn't painting, so
    // the panel could otherwise stay at opacity 0 in a backgrounded tab.)
    void state.panelEl.offsetWidth;
    state.panelEl.classList.add("is-open");
    resizeRenderer();
    setStatus("Завантаження прев'ю…");

    const cached = state.fileListCache.get(token);
    if (cached) {
      startWithFiles(token, cached);
      return;
    }

    const controller = new AbortController();
    state.controller = controller;
    Core.fetchFileList(token, controller.signal)
      .then((files) => {
        cacheSet(state.fileListCache, token, files, FILELIST_CACHE_MAX);
        if (state.token !== token) return;
        startWithFiles(token, files);
      })
      .catch(() => {
        if (controller.signal.aborted) return;
        if (state.token === token) setStatus("Не вдалося завантажити прев'ю");
      });
  }

  function closePanel() {
    if (!state.open) return;
    state.open = false;
    stopRenderLoop();
    clearMesh();
    if (state.controller) {
      state.controller.abort();
      state.controller = null;
    }
    if (state.panelEl) {
      state.panelEl.classList.remove("is-open", "is-max");
      state.panelEl.hidden = true;
    }
    state.token = null;
    state.triggerEl = null;
    state.files = [];
    state.activeIndex = -1;
  }

  // Single delegated click handler: open the panel on a folder icon, close it
  // on a click outside. The job-code copy-button ([data-copy]) is excluded so
  // the frequent "copy path-ID" click never spins up the WebGL panel.
  document.addEventListener("click", (event) => {
    const trigger = event.target.closest("[data-stl-preview-token]:not([data-copy])");
    if (trigger) {
      const token = trigger.dataset.stlPreviewToken;
      if (token) {
        event.preventDefault(); // don't navigate the folder <a>
        openPanel(trigger, token);
        return;
      }
    }
    if (state.open && state.panelEl && !event.target.closest(".stl-panel")) {
      closePanel();
    }
  });

  document.addEventListener("keydown", (event) => {
    // Перший Esc виходить із повного екрана, другий закриває панель —
    // інакше оператор, що розгорнув модель, закриває її разом із фулскріном
    // і мусить шукати ту саму теку знову.
    if (event.key !== "Escape" || !state.open) return;
    if (state.panelEl && state.panelEl.classList.contains("is-max")) {
      state.panelEl.classList.remove("is-max");
      syncMaxButton(false);
      resizeRenderer();
      return;
    }
    closePanel();
  });

  window.addEventListener("resize", () => {
    if (state.open) resizeRenderer();
  });
})();
