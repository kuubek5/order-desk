/*
 * Спільне ядро STL-прев'ю: те, що однаково потрібно і стійкій панелі черги /
 * видачі (stl-preview.js), і вбудованій галереї тріажу пошти (stl-gallery.js).
 *
 * Чому окремий файл: обидва скрипти тримали власні копії створення
 * WebGL-контексту, парсингу STL, кешу геометрій, центрування моделі й
 * звільнення контексту. Копії розходились — виправлення в одному місці не
 * долітало в друге (саме так галерея отримала forceContextLoss, а панель ні,
 * і навпаки: панель уміє зум і ручне обертання, галерея — ні). Тепер спільна
 * частина одна, а різне лишається у своїх файлах.
 *
 * Це ЗВИЧАЙНИЙ скрипт (IIFE + window), а не ES-модуль: усі скрипти проєкту
 * класичні з defer і ділять один глобальний простір (див.
 * tests/test_frontend_assets.py). Вантажиться ПІСЛЯ three.js і STLLoader,
 * але ПЕРЕД stl-preview.js / stl-gallery.js.
 *
 * «Вигляд» (view) — будь-який об'єкт із полями canvas / renderer / scene /
 * camera / mesh. Ядро не володіє станом викликача: воно дописує ці поля в
 * переданий об'єкт, тому і панель, і галерея лишають свою власну структуру
 * `state` і свою логіку відкриття, позиціювання й прибирання.
 */
(function () {
  "use strict";

  // Базове обертання за кадр (радіани) при швидкості 1.0. Слайдер панелі
  // множить це; 0 = заморожено.
  var BASE_SPIN_Y = 0.012;
  var BASE_SPIN_X = 0.003;

  // Теплий бірюзовий — запасний колір моделі, якщо тема не дала токена.
  var FALLBACK_COLOR = 0x5eead4;
  var colorCache = null;

  function available() {
    return typeof THREE !== "undefined" && typeof THREE.STLLoader !== "undefined";
  }

  function reducedMotion() {
    return !!(
      window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches
    );
  }

  // Колір моделі читається з токена --stl-model, щоб тема (theme-forge.css)
  // могла перефарбувати модель, не чіпаючи JS.
  function modelColor() {
    if (colorCache !== null) return colorCache;
    colorCache = FALLBACK_COLOR;
    try {
      var v = getComputedStyle(document.body).getPropertyValue("--stl-model").trim();
      if (/^#[0-9a-fA-F]{6}$/.test(v)) colorCache = parseInt(v.slice(1), 16);
    } catch (e) {
      /* тема ще не застосована — лишаємо запасний */
    }
    return colorCache;
  }

  // Сховище — через KMStore (storage.js): він додає спільний префікс
  // kuubmill:v1: і сам ковтає вимкнене сховище (приватний режим), повертаючи
  // null. Тут лишається тільки розбір значення й дефолти.
  function readBool(key, fallback) {
    var raw = KMStore.get(key);
    if (raw === null) return fallback;
    return raw === "1";
  }

  function writeBool(key, on) {
    KMStore.set(key, on ? "1" : "0");
  }

  function readNumber(key, min, max) {
    var raw = KMStore.get(key);
    if (raw === null) return null;
    var v = Number(raw);
    if (!Number.isFinite(v)) return null;
    return Math.min(max, Math.max(min, v));
  }

  function writeNumber(key, v) {
    KMStore.set(key, String(v));
  }

  // Один рендерер на полотно: браузер тримає лише ~16 WebGL-контекстів
  // одночасно, тож ніхто тут не створює контекст «про запас».
  function createView(view, canvas) {
    var renderer = new THREE.WebGLRenderer({
      canvas: canvas,
      antialias: true,
      alpha: true,
      preserveDrawingBuffer: false,
    });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));

    var scene = new THREE.Scene();
    var camera = new THREE.PerspectiveCamera(35, 1, 0.1, 1000);

    scene.add(new THREE.AmbientLight(0xffffff, 0.6));
    var key = new THREE.DirectionalLight(0xffffff, 0.9);
    key.position.set(2, 3, 4);
    scene.add(key);
    var rim = new THREE.DirectionalLight(modelColor(), 0.45);
    rim.position.set(-3, -2, -2);
    scene.add(rim);

    view.canvas = canvas;
    view.renderer = renderer;
    view.scene = scene;
    view.camera = camera;
    resizeView(view);
    return view;
  }

  function resizeView(view) {
    if (!view.renderer) return;
    var rect = view.canvas.getBoundingClientRect();
    var w = Math.max(1, Math.round(rect.width));
    var h = Math.max(1, Math.round(rect.height));
    view.renderer.setSize(w, h, false);
    view.camera.aspect = w / h;
    view.camera.updateProjectionMatrix();
    renderOnce(view);
  }

  function renderOnce(view) {
    if (view.renderer && view.scene && view.camera) {
      view.renderer.render(view.scene, view.camera);
    }
  }

  function clearMesh(view) {
    if (!view.mesh) return;
    if (view.scene) view.scene.remove(view.mesh);
    if (view.mesh.geometry && view.mesh.geometry.dispose) view.mesh.geometry.dispose();
    if (view.mesh.material && view.mesh.material.dispose) view.mesh.material.dispose();
    view.mesh = null;
  }

  // Ставить геометрію в кадр: нормалі, центрування, масштаб, камера.
  function showGeometry(view, geometry) {
    clearMesh(view);

    // STL з CAD/CAM часто приходять із нульовими нормалями на гранях
    // (слайсери рахують свої) — з MeshStandardMaterial кожен доданок N·L
    // тоді нуль, і модель малюється суцільно чорною, хоч і завантажилась
    // нормально. Тому нормалі рахуємо самі, з обходу трикутників.
    geometry.deleteAttribute("normal");
    geometry.computeVertexNormals();

    geometry.computeBoundingBox();
    var size = new THREE.Vector3();
    geometry.boundingBox.getSize(size);
    var maxDim = Math.max(size.x, size.y, size.z) || 1;

    // Центруємо ВЕРШИНИ геометрії, а не mesh.position: позиція живе в
    // немасштабованому просторі батька і викинула б зменшену модель за межі
    // видимої піраміди.
    geometry.center();

    var material = new THREE.MeshStandardMaterial({
      color: modelColor(),
      metalness: 0.18,
      roughness: 0.5,
    });
    var mesh = new THREE.Mesh(geometry, material);
    mesh.scale.setScalar(2.2 / maxDim);

    view.scene.add(mesh);
    view.mesh = mesh;
    view.camera.position.set(0, 0.6, 3.4);
    view.camera.lookAt(0, 0, 0);
    return mesh;
  }

  // Один крок авто-обертання. speed 1.0 = базовий крок.
  function spinMesh(mesh, speed) {
    if (!mesh) return;
    mesh.rotation.y += BASE_SPIN_Y * speed;
    mesh.rotation.x += BASE_SPIN_X * speed;
  }

  // Статус і полотно взаємовиключні: доки є текст, полотно ховаємо, щоб напис
  // не читався поверх напівнамальованої моделі.
  function applyStatus(statusEl, canvas, text) {
    if (!statusEl) return;
    statusEl.textContent = text || "";
    statusEl.style.display = text ? "flex" : "none";
    if (canvas) canvas.style.visibility = text ? "hidden" : "visible";
  }

  // Токен непрозорий: ядро ніколи не розбирає і не будує з нього шляхів, лише
  // повертає назад у /stl-preview/... — безпека шляхів на сервері
  // (app/stl_preview.py).
  function fetchFileList(token, signal) {
    return fetch("/stl-preview/" + encodeURIComponent(token), { signal: signal })
      .then(function (response) {
        if (!response.ok) throw new Error("list-failed");
        return response.json();
      })
      .then(function (data) {
        return Array.isArray(data.files) ? data.files : [];
      });
  }

  function fetchGeometry(token, filename, signal) {
    return fetch(
      "/stl-preview/" + encodeURIComponent(token) + "/" + encodeURIComponent(filename),
      { signal: signal }
    )
      .then(function (response) {
        if (!response.ok) throw new Error("file-failed");
        return response.arrayBuffer();
      })
      .then(function (buffer) {
        return new THREE.STLLoader().parse(buffer);
      });
  }

  var FILE_ICON =
    '<path d="M12 2 2 7.5v9L12 22l10-5.5v-9L12 2z"/><path d="M2 7.5 12 13l10-5.5M12 13v9"/>';

  // Список .stl у папці. Різниця між панеллю і галереєю — лише клас кнопки,
  // тому він приходить параметром.
  function renderFileList(container, files, activeIndex, cls) {
    container.innerHTML = "";
    files.forEach(function (filename, i) {
      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = cls + (i === activeIndex ? " is-active" : "");
      btn.dataset.index = String(i);
      btn.setAttribute("role", "listitem");
      btn.setAttribute("aria-pressed", i === activeIndex ? "true" : "false");

      var icon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      icon.setAttribute("viewBox", "0 0 24 24");
      icon.setAttribute("aria-hidden", "true");
      icon.innerHTML = FILE_ICON;
      btn.appendChild(icon);

      var name = document.createElement("span");
      name.className = "name mono";
      name.textContent = filename;
      btn.appendChild(name);

      container.appendChild(btn);
    });
  }

  function updateActiveFile(container, cls, index) {
    var buttons = container.querySelectorAll("." + cls);
    buttons.forEach(function (btn, i) {
      var active = i === index;
      btn.classList.toggle("is-active", active);
      btn.setAttribute("aria-pressed", active ? "true" : "false");
    });
  }

  function disposeGeometries(cache) {
    if (!cache) return;
    cache.forEach(function (geometry) {
      if (geometry && geometry.dispose) geometry.dispose();
    });
    cache.clear();
  }

  // forceContextLoss звільняє контекст ОДРАЗУ, не чекаючи збирача сміття —
  // саме ліміт ~16 контекстів тут і впирається. Без цього прев'ю мовчки
  // переставало малюватись після пари десятків переглянутих листів, і оператор
  // бачив «глючить», а не помилку.
  function disposeView(view) {
    clearMesh(view);
    if (view.renderer) {
      view.renderer.dispose();
      if (view.renderer.forceContextLoss) view.renderer.forceContextLoss();
      view.renderer = null;
    }
    view.scene = null;
    view.camera = null;
  }

  // Реєстр живих полотен сторінки. HTMX не повідомляє про смерть вузла, тож
  // єдиний надійний момент прибрати — наступний свап: те, чого вже немає в
  // документі, більше ніколи не оживе.
  var live = [];

  function track(entry) {
    live.push(entry);
  }

  function sweep() {
    for (var i = live.length - 1; i >= 0; i -= 1) {
      if (!document.contains(live[i].root)) {
        try {
          live[i].dispose();
        } catch (err) {
          /* прибирання не має ламати свап */
        }
        live.splice(i, 1);
      }
    }
  }

  window.StlRenderCore = {
    BASE_SPIN_Y: BASE_SPIN_Y,
    BASE_SPIN_X: BASE_SPIN_X,
    available: available,
    reducedMotion: reducedMotion,
    modelColor: modelColor,
    readBool: readBool,
    writeBool: writeBool,
    readNumber: readNumber,
    writeNumber: writeNumber,
    createView: createView,
    resizeView: resizeView,
    renderOnce: renderOnce,
    clearMesh: clearMesh,
    showGeometry: showGeometry,
    spinMesh: spinMesh,
    applyStatus: applyStatus,
    fetchFileList: fetchFileList,
    fetchGeometry: fetchGeometry,
    renderFileList: renderFileList,
    updateActiveFile: updateActiveFile,
    disposeGeometries: disposeGeometries,
    disposeView: disposeView,
    track: track,
    sweep: sweep,
  };
})();
