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

  if (typeof THREE === "undefined" || typeof THREE.STLLoader === "undefined") {
    return;
  }

  const REDUCED_MOTION =
    window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // Base per-frame rotation (radians) at slider position 1.0. The speed slider
  // scales this; 0 = frozen. Default respects prefers-reduced-motion (starts
  // still) but the slider lets the operator opt back into spin.
  const BASE_SPIN_Y = 0.012;
  const BASE_SPIN_X = 0.003;

  // Warm teal accent that stays visible against the dark v2a stage; normals
  // are recomputed below so the mesh is never solid black regardless of STL.
  const MODEL_COLOR = 0x5eead4;

  const state = {
    token: null,
    triggerEl: null,
    folderUri: null,
    files: [],
    activeIndex: -1,
    controller: null,
    rafId: null,
    spinSpeed: REDUCED_MOTION ? 0 : 1, // multiplier on BASE_SPIN_*, driven by the slider
    dragging: false, // права кнопка затиснута — ручне обертання
    dragLastX: 0,
    dragLastY: 0,
    speedEl: null,
    fileListCache: new Map(), // token -> string[]
    geometryCache: new Map(), // "token filename" -> BufferGeometry (raw)
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

    const renderer = new THREE.WebGLRenderer({
      canvas: state.canvasEl,
      antialias: true,
      alpha: true,
      preserveDrawingBuffer: false,
    });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));

    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(35, 1, 0.1, 1000);

    scene.add(new THREE.AmbientLight(0xffffff, 0.6));
    const key = new THREE.DirectionalLight(0xffffff, 0.9);
    key.position.set(2, 3, 4);
    scene.add(key);
    const rim = new THREE.DirectionalLight(0x5eead4, 0.45);
    rim.position.set(-3, -2, -2);
    scene.add(rim);

    state.renderer = renderer;
    state.scene = scene;
    state.camera = camera;
    resizeRenderer();
  }

  function resizeRenderer() {
    if (!state.renderer) return;
    const rect = state.canvasEl.getBoundingClientRect();
    const w = Math.max(1, Math.round(rect.width));
    const h = Math.max(1, Math.round(rect.height));
    state.renderer.setSize(w, h, false);
    state.camera.aspect = w / h;
    state.camera.updateProjectionMatrix();
    renderOnce();
  }

  function setStatus(text) {
    ensurePanel();
    state.statusEl.textContent = text || "";
    state.statusEl.style.display = text ? "flex" : "none";
    state.canvasEl.style.visibility = text ? "hidden" : "visible";
  }

  function clearMesh() {
    if (state.mesh) {
      state.scene.remove(state.mesh);
      state.mesh.geometry?.dispose?.();
      state.mesh.material?.dispose?.();
      state.mesh = null;
    }
  }

  function renderOnce() {
    if (state.renderer && state.scene && state.camera) {
      state.renderer.render(state.scene, state.camera);
    }
  }

  function showGeometry(geometry) {
    ensureRenderer();
    clearMesh();

    // CAD/CAM-exported STLs often ship zero/degenerate per-facet normals
    // (slicers recompute their own) — with MeshStandardMaterial every N·L term
    // is then zero and the mesh renders solid black despite loading fine.
    // Always recompute from the triangle winding.
    geometry.deleteAttribute("normal");
    geometry.computeVertexNormals();

    geometry.computeBoundingBox();
    const size = new THREE.Vector3();
    geometry.boundingBox.getSize(size);
    const maxDim = Math.max(size.x, size.y, size.z) || 1;

    // Center the GEOMETRY's own vertices (not mesh.position, which lives in
    // unscaled parent space and would fling the scaled-down mesh out of the
    // frustum). See the long note the mail gallery/preview history carries.
    geometry.center();

    const material = new THREE.MeshStandardMaterial({
      color: MODEL_COLOR,
      metalness: 0.18,
      roughness: 0.5,
    });
    const mesh = new THREE.Mesh(geometry, material);
    mesh.scale.setScalar(2.2 / maxDim);

    state.scene.add(mesh);
    state.mesh = mesh;
    state.camera.position.set(0, 0.6, 3.4);
    state.camera.lookAt(0, 0, 0);

    setStatus(null);
    startRenderLoop();
  }

  // Ручне обертання правою кнопкою (прохання оператора 28.08.26). Клік правою
  // одразу глушить авто-обертання — щоб звіряти форму коронки зі STL, модель
  // має стояти рівно там, де оператор її лишив, а не крутитись під рукою.
  // Утримання правої + рух — крутить модель у двох осях. Ліве перетягування
  // теж крутить (звична дія), тому ловимо будь-яку кнопку.
  const DRAG_SENS = 0.01; // радіан на піксель

  function freezeSpin() {
    state.spinSpeed = 0;
    if (state.speedEl) state.speedEl.value = "0"; // слайдер показує реальний стан
    stopRenderLoop();
    renderOnce();
  }

  function attachManualRotation(canvas) {
    // Права кнопка не має відкривати системне меню поверх моделі.
    canvas.addEventListener("contextmenu", (event) => event.preventDefault());

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
      // Горизонтальний рух — навколо вертикалі, вертикальний — навколо горизонталі.
      state.mesh.rotation.y += dx * DRAG_SENS;
      state.mesh.rotation.x += dy * DRAG_SENS;
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
      if (state.mesh) {
        state.mesh.rotation.y += BASE_SPIN_Y * state.spinSpeed;
        state.mesh.rotation.x += BASE_SPIN_X * state.spinSpeed;
      }
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

  function renderFileList() {
    ensurePanel();
    state.filesEl.innerHTML = "";
    state.files.forEach((filename, i) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "stl-panel-file" + (i === state.activeIndex ? " is-active" : "");
      btn.dataset.index = String(i);
      btn.setAttribute("role", "listitem");
      btn.setAttribute("aria-pressed", i === state.activeIndex ? "true" : "false");

      const icon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      icon.setAttribute("viewBox", "0 0 24 24");
      icon.setAttribute("aria-hidden", "true");
      icon.innerHTML =
        '<path d="M12 2 2 7.5v9L12 22l10-5.5v-9L12 2z"/><path d="M2 7.5 12 13l10-5.5M12 13v9"/>';
      btn.appendChild(icon);

      const name = document.createElement("span");
      name.className = "name mono";
      name.textContent = filename;
      btn.appendChild(name);

      state.filesEl.appendChild(btn);
    });
  }

  function updateActiveFile(index) {
    const buttons = state.filesEl.querySelectorAll(".stl-panel-file");
    buttons.forEach((btn, i) => {
      const active = i === index;
      btn.classList.toggle("is-active", active);
      btn.setAttribute("aria-pressed", active ? "true" : "false");
    });
  }

  function fetchGeometry(token, filename, signal) {
    return fetch(
      `/stl-preview/${encodeURIComponent(token)}/${encodeURIComponent(filename)}`,
      { signal }
    )
      .then((response) => {
        if (!response.ok) throw new Error("file-failed");
        return response.arrayBuffer();
      })
      .then((buffer) => new THREE.STLLoader().parse(buffer));
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

    fetchGeometry(token, filename, controller.signal)
      .then((geometry) => {
        if (controller.signal.aborted) return;
        state.geometryCache.set(geoKey(token, filename), geometry);
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
    fetch(`/stl-preview/${encodeURIComponent(token)}`, { signal: controller.signal })
      .then((response) => {
        if (!response.ok) throw new Error("list-failed");
        return response.json();
      })
      .then((data) => {
        const files = Array.isArray(data.files) ? data.files : [];
        state.fileListCache.set(token, files);
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
      state.panelEl.classList.remove("is-open");
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
    if (event.key === "Escape" && state.open) closePanel();
  });

  window.addEventListener("resize", () => {
    if (state.open) resizeRenderer();
  });
})();
