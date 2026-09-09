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

  // Спільне ядро (stl-render-core.js) тримає все, що однакове з панеллю
  // прев'ю: WebGL-контекст, парсинг STL, центрування моделі, колір із токена,
  // звільнення контексту. Немає ядра або three.js — галереї немає.
  const Core = window.StlRenderCore;
  if (!Core || !Core.available()) {
    return;
  }

  const REDUCED_MOTION = Core.reducedMotion();

  // Розгортання на весь екран (аудит 05.09.26, UX 1.5). Панель прев'ю на
  // видачі відкривається розгорнутою за замовчуванням, бо там звірка форми —
  // єдина робота оператора. Тут інакше: тріаж — це заповнення полів ПОРУЧ із
  // моделлю, тож автоматичний фулскрін перекривав би саму форму. Тому кнопка
  // є, вибір запам'ятовується, але дефолт — вбудований вигляд.
  const MAX_STORAGE_KEY = "stl-gallery-max";

  // Дефолт живе САМЕ ТУТ, а не в ядрі: тріаж без збереженого вибору
  // відкривається вбудованим, панель видачі — розгорнутою.
  function loadMaxPreference() {
    return Core.readBool(MAX_STORAGE_KEY, false);
  }

  function saveMaxPreference(on) {
    Core.writeBool(MAX_STORAGE_KEY, on);
  }

  function setupGallery(root) {
    if (root.dataset.galleryInit) return; // already wired (e.g. re-scanned after an HTMX swap)
    root.dataset.galleryInit = "1";
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
      Core.applyStatus(statusEl, canvas, text);
    }

    function ensureRenderer() {
      if (state.renderer) return;
      // Ядро дописує canvas/renderer/scene/camera прямо в state.
      Core.createView(state, canvas);
    }

    function resizeRenderer() {
      Core.resizeView(state);
    }

    function renderOnce() {
      Core.renderOnce(state);
    }

    function showGeometry(geometry) {
      ensureRenderer();
      // Нормалі, центрування, масштаб і камера — у ядрі (спільне з панеллю).
      Core.showGeometry(state, geometry);
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
        Core.spinMesh(state.mesh, 1); // галерея без слайдера — завжди базова швидкість
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

    // Розмітка списку файлів спільна з панеллю; різниться лише клас кнопки.
    const FILE_CLASS = "stl-gallery-file";

    function renderFileList() {
      Core.renderFileList(filesEl, state.files, state.activeIndex, FILE_CLASS);
    }

    function updateActiveFile(index) {
      Core.updateActiveFile(filesEl, FILE_CLASS, index);
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

      Core.fetchGeometry(token, filename, controller.signal)
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

    // Розгортання на весь екран: та сама роль, що й у панелі прев'ю на видачі.
    const maxBtn = root.querySelector(".stl-gallery-max");
    function applyMax(on) {
      root.classList.toggle("is-max", on);
      if (maxBtn) {
        maxBtn.setAttribute("aria-label", on ? "Згорнути" : "Розгорнути на весь екран");
        maxBtn.title = maxBtn.getAttribute("aria-label");
        maxBtn.setAttribute("aria-pressed", on ? "true" : "false");
      }
      resizeRenderer();
    }
    if (maxBtn) {
      maxBtn.addEventListener("click", () => {
        const on = !root.classList.contains("is-max");
        applyMax(on);
        saveMaxPreference(on);
      });
      applyMax(loadMaxPreference());
    }
    // Esc виходить із фулскріна, але вибір НЕ переписує: це разове «згорнути
    // зараз», а не зміна звички (той самий контракт, що в stl-preview.js).
    function onKeydown(event) {
      if (event.key !== "Escape") return;
      if (!root.classList.contains("is-max")) return;
      applyMax(false);
    }
    document.addEventListener("keydown", onKeydown);

    // Keep the render crisp when the layout width changes.
    let resizeObserver = null;
    if (typeof ResizeObserver !== "undefined") {
      resizeObserver = new ResizeObserver(() => resizeRenderer());
      resizeObserver.observe(canvas);
    } else {
      window.addEventListener("resize", resizeRenderer);
    }

    // Kick off: fetch the STL file list for this token.
    const listController = new AbortController();

    // Прибирання за собою, коли панель зникла з DOM (Core.sweep на кожен
    // свап). Без цього кожен клік по листу в тріажі лишав ЖИВИЙ WebGLRenderer на
    // викинутому <canvas>: браузер тримає лише ~16 контекстів одночасно, тож
    // після пари десятків переглянутих листів прев'ю мовчки переставало
    // малюватись — оператор бачив «глючить», а не помилку.
    Core.track({
      root,
      dispose() {
        stopRenderLoop();
        if (state.controller) state.controller.abort();
        listController.abort();
        if (resizeObserver) resizeObserver.disconnect();
        else window.removeEventListener("resize", resizeRenderer);
        document.removeEventListener("keydown", onKeydown);
        Core.disposeGeometries(state.geometryCache);
        // disposeView знімає меш і робить forceContextLoss.
        Core.disposeView(state);
      },
    });
    setStatus("Завантаження прев'ю…");
    Core.fetchFileList(token, listController.signal)
      .then((files) => {
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
    Core.sweep();
    const roots = document.querySelectorAll("[data-stl-gallery-token]");
    roots.forEach(setupGallery);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  // The triage detail panel arrives via an HTMX swap (mail_triage.html loads
  // _mail_detail_panel.html into #mail-detail on a row click), so its gallery
  // isn't in the DOM at load. Re-scan after every settle — setupGallery's
  // dataset guard keeps already-wired galleries from being set up twice.
  document.body.addEventListener("htmx:afterSettle", init);
})();
