/*
 * Inline STL gallery for the mail triage card (app/templates/mail_detail.html).
 *
 * Unlike the floating hover popup in stl-preview.js (still used by the folder
 * icons in the queue, see _order_row.html), this renders a single, always-in-
 * place gallery embedded in the triage card: a large 3D preview plus a click-
 * able list of the .stl files in the mail's attachment folder. Clicking a file
 * name swaps the model shown in the one shared WebGL canvas.
 *
 * Design notes:
 *  - The token is opaque; this script never inspects or builds paths from it,
 *    it only round-trips it back to /stl-preview/{token}[/{filename}] (list +
 *    bytes routes, path safety enforced in app/stl_preview.py).
 *  - One WebGL context per gallery (browsers cap concurrent contexts). A page
 *    only ever has one gallery (a single mail card), but the code supports
 *    several defensively.
 *  - Everything is abortable: a pending fetch is cancelled before starting the
 *    next, and geometries are cached per filename so re-selecting a file is
 *    instant.
 *  - Respects prefers-reduced-motion: the model doesn't auto-rotate when the
 *    user asked to reduce motion (a single static render is drawn instead of a
 *    RAF loop).
 *  - No-op on any page without `[data-stl-gallery-token]`.
 */
(function () {
  "use strict";

  if (typeof THREE === "undefined" || typeof THREE.STLLoader === "undefined") {
    return;
  }

  const REDUCED_MOTION =
    window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // Warm accent that stays visible against the dark v2a stage; normals are
  // recomputed below so the mesh is never solid black regardless of the STL.
  const MODEL_COLOR = 0x5eead4;

  function setupGallery(root) {
    const token = root.dataset.stlGalleryToken;
    if (!token) return;

    const section = root.closest(".stl-gallery-sec") || root;
    const canvas = root.querySelector(".stl-gallery-canvas");
    const statusEl = root.querySelector(".stl-gallery-status");
    const filesEl = root.querySelector(".stl-gallery-files");
    if (!canvas || !filesEl) return;

    const state = {
      files: [],
      activeIndex: -1,
      controller: null,
      rafId: null,
      renderer: null,
      scene: null,
      camera: null,
      mesh: null,
      geometryCache: new Map(), // filename -> BufferGeometry (raw)
    };

    function setStatus(text) {
      if (!statusEl) return;
      statusEl.textContent = text || "";
      statusEl.style.display = text ? "flex" : "none";
      canvas.style.visibility = text ? "hidden" : "visible";
    }

    function ensureRenderer() {
      if (state.renderer) return;

      const renderer = new THREE.WebGLRenderer({
        canvas,
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
      const rect = canvas.getBoundingClientRect();
      const w = Math.max(1, Math.round(rect.width));
      const h = Math.max(1, Math.round(rect.height));
      state.renderer.setSize(w, h, false);
      state.camera.aspect = w / h;
      state.camera.updateProjectionMatrix();
      renderOnce();
    }

    function clearMesh() {
      if (state.mesh) {
        state.scene.remove(state.mesh);
        state.mesh.geometry && state.mesh.geometry.dispose && state.mesh.geometry.dispose();
        state.mesh.material && state.mesh.material.dispose && state.mesh.material.dispose();
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
      // (slicers recompute their own) — with MeshStandardMaterial that makes
      // every N·L term zero and the mesh renders solid black despite loading
      // fine. Always recompute from the triangle winding.
      geometry.deleteAttribute("normal");
      geometry.computeVertexNormals();

      geometry.computeBoundingBox();
      const size = new THREE.Vector3();
      geometry.boundingBox.getSize(size);
      const maxDim = Math.max(size.x, size.y, size.z) || 1;

      // Center the GEOMETRY's own vertices (not mesh.position, which lives in
      // unscaled parent space and would fling the scaled-down mesh out of the
      // frustum). See the long note in stl-preview.js for the full reasoning.
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

    function startRenderLoop() {
      stopRenderLoop();
      if (REDUCED_MOTION) {
        renderOnce();
        return;
      }
      function tick() {
        if (!document.body.contains(canvas)) {
          stopRenderLoop();
          return;
        }
        if (state.mesh) {
          state.mesh.rotation.y += 0.012;
          state.mesh.rotation.x += 0.003;
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
      filesEl.innerHTML = "";
      state.files.forEach((filename, i) => {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "stl-gallery-file" + (i === state.activeIndex ? " is-active" : "");
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

        filesEl.appendChild(btn);
      });
    }

    function updateActiveFile(index) {
      const buttons = filesEl.querySelectorAll(".stl-gallery-file");
      buttons.forEach((btn, i) => {
        const active = i === index;
        btn.classList.toggle("is-active", active);
        btn.setAttribute("aria-pressed", active ? "true" : "false");
      });
    }

    function fetchGeometry(filename, signal) {
      return fetch(
        `/stl-preview/${encodeURIComponent(token)}/${encodeURIComponent(filename)}`,
        { signal }
      ).then((response) => {
        if (!response.ok) throw new Error("file-failed");
        return response.arrayBuffer();
      }).then((buffer) => new THREE.STLLoader().parse(buffer));
    }

    function selectFile(index) {
      if (index < 0 || index >= state.files.length) return;
      state.activeIndex = index;
      updateActiveFile(index);

      const filename = state.files[index];
      const cached = state.geometryCache.get(filename);
      if (cached) {
        showGeometry(cached.clone());
        return;
      }

      ensureRenderer();
      setStatus("Завантаження…");

      if (state.controller) state.controller.abort();
      const controller = new AbortController();
      state.controller = controller;

      fetchGeometry(filename, controller.signal)
        .then((geometry) => {
          if (controller.signal.aborted) return;
          state.geometryCache.set(filename, geometry);
          if (state.activeIndex !== index) return; // switched away meanwhile
          showGeometry(geometry.clone());
        })
        .catch(() => {
          if (controller.signal.aborted) return;
          if (state.activeIndex === index) setStatus("Не вдалося завантажити прев'ю");
        });
    }

    filesEl.addEventListener("click", (event) => {
      const btn = event.target.closest(".stl-gallery-file");
      if (!btn) return;
      const index = Number(btn.dataset.index);
      if (index === state.activeIndex) return;
      selectFile(index);
    });

    // Keep the render crisp when the layout width changes.
    if (typeof ResizeObserver !== "undefined") {
      const ro = new ResizeObserver(() => resizeRenderer());
      ro.observe(canvas);
    } else {
      window.addEventListener("resize", resizeRenderer);
    }

    // Kick off: fetch the STL file list for this token.
    const listController = new AbortController();
    setStatus("Завантаження прев'ю…");
    fetch(`/stl-preview/${encodeURIComponent(token)}`, { signal: listController.signal })
      .then((response) => {
        if (!response.ok) throw new Error("list-failed");
        return response.json();
      })
      .then((data) => {
        const files = Array.isArray(data.files) ? data.files : [];
        if (files.length === 0) {
          // No STL after all — drop the whole gallery section, the plain
          // attachment list stays as the record of files.
          section.hidden = true;
          return;
        }
        state.files = files;
        renderFileList();
        selectFile(0);
      })
      .catch(() => {
        setStatus("Не вдалося завантажити прев'ю");
      });
  }

  function init() {
    const roots = document.querySelectorAll("[data-stl-gallery-token]");
    roots.forEach(setupGallery);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
