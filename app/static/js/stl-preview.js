/*
 * Hover STL preview for "📂 відкрити папку" links.
 *
 * Any element carrying `data-stl-preview-token="<token>"` gets a debounced
 * hover popup that lists the folder's .stl files (server route in
 * app/web.py, path safety in app/stl_preview.py) and renders the first one
 * with three.js (vendored, see THIRD_PARTY_NOTICES.md).
 *
 * Design notes:
 *  - The token is opaque; this script never inspects or builds paths from
 *    it, it only round-trips it back to /stl-preview/{token}[/{filename}].
 *  - Debounced ~250ms so quickly passing the cursor over several folder
 *    icons in the queue table doesn't fire a burst of fetches.
 *  - One reusable WebGL context/popup for the whole page (not one per
 *    link) — browsers cap concurrent WebGL contexts, and the queue table
 *    can have dozens of folder icons.
 *  - Everything is abortable: leaving the element before the debounce or
 *    the fetch resolves cancels cleanly, no stale popups.
 */
(function () {
  "use strict";

  if (typeof THREE === "undefined" || typeof THREE.STLLoader === "undefined") {
    return;
  }

  const HOVER_DEBOUNCE_MS = 250;
  const POPUP_SIZE = 200;

  const state = {
    hoverTimer: null,
    controller: null,
    activeEl: null,
    rafId: null,
    geometryCache: new Map(), // token -> BufferGeometry | "empty" | Promise
    renderer: null,
    scene: null,
    camera: null,
    mesh: null,
    popupEl: null,
    canvasEl: null,
    statusEl: null,
  };

  function ensurePopup() {
    if (state.popupEl) return;

    const popup = document.createElement("div");
    popup.className = "stl-preview-popup";
    popup.setAttribute("aria-hidden", "true");

    const canvas = document.createElement("canvas");
    canvas.width = POPUP_SIZE;
    canvas.height = POPUP_SIZE;
    popup.appendChild(canvas);

    const status = document.createElement("div");
    status.className = "stl-preview-status";
    popup.appendChild(status);

    document.body.appendChild(popup);

    const renderer = new THREE.WebGLRenderer({
      canvas,
      antialias: true,
      alpha: true,
      preserveDrawingBuffer: false,
    });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    renderer.setSize(POPUP_SIZE, POPUP_SIZE, false);

    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(35, 1, 0.1, 1000);

    scene.add(new THREE.AmbientLight(0xffffff, 0.55));
    const key = new THREE.DirectionalLight(0xffffff, 0.9);
    key.position.set(2, 3, 4);
    scene.add(key);
    const rim = new THREE.DirectionalLight(0xe8b4a8, 0.4);
    rim.position.set(-3, -2, -2);
    scene.add(rim);

    state.popupEl = popup;
    state.canvasEl = canvas;
    state.statusEl = status;
    state.renderer = renderer;
    state.scene = scene;
    state.camera = camera;
  }

  function setStatus(text) {
    ensurePopup();
    state.statusEl.textContent = text || "";
    state.statusEl.style.display = text ? "flex" : "none";
    state.canvasEl.style.visibility = text ? "hidden" : "visible";
  }

  function positionPopup(target) {
    ensurePopup();
    const rect = target.getBoundingClientRect();
    const margin = 10;
    let left = rect.right + margin;
    let top = rect.top - POPUP_SIZE / 2 + rect.height / 2;

    const maxLeft = window.innerWidth - POPUP_SIZE - margin;
    if (left > maxLeft) {
      left = rect.left - POPUP_SIZE - margin;
    }
    left = Math.max(margin, Math.min(left, window.innerWidth - POPUP_SIZE - margin));
    top = Math.max(margin, Math.min(top, window.innerHeight - POPUP_SIZE - margin));

    state.popupEl.style.left = `${left}px`;
    state.popupEl.style.top = `${top}px`;
  }

  function clearMesh() {
    if (state.mesh) {
      state.scene.remove(state.mesh);
      state.mesh.geometry?.dispose?.();
      state.mesh.material?.dispose?.();
      state.mesh = null;
    }
  }

  function showGeometry(geometry) {
    ensurePopup();
    clearMesh();

    // Real CAD/CAM-exported STLs often ship zero/degenerate per-facet
    // normals (common for files meant for slicers, which recompute their
    // own) — with MeshStandardMaterial that makes every N·L term zero and
    // the mesh renders solid black despite loading correctly. Always
    // recompute from the triangle winding instead of trusting the file.
    geometry.deleteAttribute("normal");
    geometry.computeVertexNormals();

    geometry.computeBoundingBox();
    const size = new THREE.Vector3();
    geometry.boundingBox.getSize(size);
    const maxDim = Math.max(size.x, size.y, size.z) || 1;

    // Real scans/CAD exports rarely sit at the origin (this crown's own
    // bounding box is centered ~20 units away). Center() translates the
    // GEOMETRY's own vertices, which happens before scale/position in the
    // transform pipeline. Centering via mesh.position instead (as this used
    // to) silently breaks: Object3D.position lives in the PARENT's
    // (unscaled) space, so subtracting an unscaled real-world-sized offset
    // moves the whole (already scaled-down) mesh miles outside the camera
    // frustum — geometry loads and "renders" without error, just onto pixels
    // no camera ray ever reaches. Centering the geometry itself sidesteps
    // the ordering entirely.
    geometry.center();

    const material = new THREE.MeshStandardMaterial({
      color: 0xe8b4a8,
      metalness: 0.15,
      roughness: 0.55,
    });
    const mesh = new THREE.Mesh(geometry, material);

    const scale = 2.2 / maxDim;
    mesh.scale.setScalar(scale);

    state.scene.add(mesh);
    state.mesh = mesh;
    state.camera.position.set(0, 0.6, 3.4);
    state.camera.lookAt(0, 0, 0);

    setStatus(null);
    startRenderLoop();
  }

  function startRenderLoop() {
    stopRenderLoop();

    function tick() {
      if (!state.activeEl || !document.body.contains(state.activeEl)) {
        hidePopup();
        return;
      }
      if (state.mesh) {
        state.mesh.rotation.y += 0.012;
        state.mesh.rotation.x += 0.003;
      }
      state.renderer.render(state.scene, state.camera);
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

  function hidePopup() {
    stopRenderLoop();
    clearMesh();
    if (state.popupEl) {
      state.popupEl.classList.remove("is-visible");
    }
    state.activeEl = null;
  }

  function abortPending() {
    if (state.hoverTimer !== null) {
      window.clearTimeout(state.hoverTimer);
      state.hoverTimer = null;
    }
    if (state.controller) {
      state.controller.abort();
      state.controller = null;
    }
  }

  async function loadFirstStl(token, signal) {
    const listResponse = await fetch(`/stl-preview/${encodeURIComponent(token)}`, { signal });
    if (!listResponse.ok) throw new Error("list-failed");
    const data = await listResponse.json();
    const files = Array.isArray(data.files) ? data.files : [];
    if (files.length === 0) return null;

    const fileResponse = await fetch(
      `/stl-preview/${encodeURIComponent(token)}/${encodeURIComponent(files[0])}`,
      { signal }
    );
    if (!fileResponse.ok) throw new Error("file-failed");
    const buffer = await fileResponse.arrayBuffer();

    const loader = new THREE.STLLoader();
    return loader.parse(buffer);
  }

  function beginHover(el, token) {
    abortPending();
    state.activeEl = el;
    ensurePopup();
    positionPopup(el);
    state.popupEl.classList.add("is-visible");
    setStatus("Завантаження…");

    const cached = state.geometryCache.get(token);
    if (cached && cached !== "empty" && !(cached instanceof Promise)) {
      showGeometry(cached.clone());
      return;
    }
    if (cached === "empty") {
      setStatus("Немає STL у папці");
      return;
    }

    const controller = new AbortController();
    state.controller = controller;

    loadFirstStl(token, controller.signal)
      .then((geometry) => {
        if (state.activeEl !== el || controller.signal.aborted) return;
        if (!geometry) {
          state.geometryCache.set(token, "empty");
          setStatus("Немає STL у папці");
          return;
        }
        state.geometryCache.set(token, geometry);
        showGeometry(geometry.clone());
      })
      .catch((error) => {
        if (controller.signal.aborted) return;
        if (state.activeEl === el) {
          setStatus("Не вдалося завантажити прев'ю");
        }
      });
  }

  function endHover(el) {
    abortPending();
    if (state.activeEl === el) {
      hidePopup();
    }
  }

  document.addEventListener(
    "mouseover",
    (event) => {
      const el = event.target.closest("[data-stl-preview-token]");
      if (!el || el === state.activeEl) return;
      const token = el.dataset.stlPreviewToken;
      if (!token) return;

      abortPending();
      state.hoverTimer = window.setTimeout(() => {
        state.hoverTimer = null;
        beginHover(el, token);
      }, HOVER_DEBOUNCE_MS);
    },
    true
  );

  document.addEventListener(
    "mouseout",
    (event) => {
      const el = event.target.closest("[data-stl-preview-token]");
      if (!el) return;
      const related = event.relatedTarget;
      if (related && el.contains(related)) return;
      endHover(el);
    },
    true
  );

  window.addEventListener("scroll", () => endHover(state.activeEl), true);
  window.addEventListener("blur", () => endHover(state.activeEl));
})();
