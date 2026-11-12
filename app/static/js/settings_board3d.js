/* Налаштування → Табло → «3D на телевізорі»: живе прев'ю (власник 12.11.26).

   У рамці — справжнє табло цеху (`/settings/feedback/board/3d/preview`, той
   самий шаблон і дані). Кожен рух повзунка чи перемикача одразу йде в
   прев'ю через `KM3D.setConfig` (той самий origin), у базу — лише кнопкою
   «Зберегти». «Скасувати зміни» повертає форму й прев'ю до збереженого,
   «Типові» ставить типові у форму (зберегти — тією ж кнопкою).
   Масштаб: «Усе табло» — рамка 1920×1003 вписана в ширину; «Плитка крупно» —
   перша плитка з 3D на всю ширину. */
(function(){
  "use strict";
  var form = document.querySelector("[data-b3d]");
  if (!form) return;
  var stage = form.querySelector("[data-b3d-stage]"), frame = stage.querySelector("iframe");
  var dirtyBadge = form.querySelector("[data-b3d-dirty]"), empty = form.querySelector("[data-b3d-empty]");
  var zoom = "board";

  function inputs(){ return Array.prototype.slice.call(form.querySelectorAll("input[name]")); }
  function label(input){
    var out = input.parentNode.querySelector("output");
    if (out) out.value = String(Number(input.value)) + (input.getAttribute("data-unit") || "");
  }
  function values(){
    var v = {};
    inputs().forEach(function(i){ v[i.name] = i.type === "checkbox" ? i.checked : Number(i.value); });
    return v;
  }
  var saved = JSON.stringify(values());
  var snapshot = inputs().map(function(i){ return i.type === "checkbox" ? i.checked : i.value; });

  function km3d(){
    try { return frame.contentWindow && frame.contentWindow.KM3D; } catch (_e) { return null; }
  }
  function push(){
    var now = values(), dirty = JSON.stringify(now) !== saved;
    dirtyBadge.hidden = !dirty;
    var api = km3d();
    if (api) api.setConfig(dirty ? now : null);
    // Висота смуги «Світло» — не 3D: прямо змінна на `.board` прев'ю.
    try {
      var board = frame.contentDocument && frame.contentDocument.querySelector(".board");
      if (board && now.pw_k) board.style.setProperty("--pw-k", String(now.pw_k));
    } catch (_e) { /* рамка ще не готова — наступний рух повзунка */ }
    // Велика модель і малі можуть з'явитись/зникнути — масштаб «плитка» переміряти.
    if (zoom === "tile") setTimeout(fit, 60);
  }
  function fit(){
    var w = stage.clientWidth || 1, doc = null;
    try { doc = frame.contentDocument; } catch (_e) { doc = null; }
    var tile = doc && doc.querySelector(".mc.has-3d");
    if (zoom === "tile" && tile){
      var r = tile.getBoundingClientRect(), k = Math.min(w / r.width, 1.6);
      stage.style.setProperty("--b3d-t", "scale(" + k + ") translate(" + (-r.left) + "px," + (-r.top) + "px)");
      stage.style.setProperty("--b3d-h", Math.round(r.height * k) + "px");
    } else {
      var s = w / 1920;
      stage.style.setProperty("--b3d-t", "scale(" + s + ")");
      stage.style.setProperty("--b3d-h", Math.round(1003 * s) + "px");
    }
  }

  form.addEventListener("input", function(e){ if (e.target.type === "range") label(e.target); push(); });
  form.addEventListener("change", push);
  form.querySelector("[data-b3d-revert]").addEventListener("click", function(){
    inputs().forEach(function(i, n){ if (i.type === "checkbox") i.checked = snapshot[n]; else { i.value = snapshot[n]; label(i); } });
    push();
  });
  form.querySelector("[data-b3d-defaults]").addEventListener("click", function(){
    inputs().forEach(function(i){
      var d = i.getAttribute("data-default");
      if (i.type === "checkbox") i.checked = d === "1"; else { i.value = d; label(i); }
    });
    push();
  });
  form.querySelectorAll("[data-b3d-zoom]").forEach(function(b){
    b.addEventListener("click", function(){
      zoom = b.getAttribute("data-b3d-zoom");
      form.querySelectorAll("[data-b3d-zoom]").forEach(function(x){ x.classList.toggle("is-on", x === b); });
      fit();
    });
  });
  // Рамка могла завантажитись раніше за цей скрипт (подія `load` уже минула),
  // а моделі вантажаться після неї — тож просто дивимось раз на пів секунди,
  // доки не з'явиться плитка з 3D (до 20 с). «Моделей немає» — лише в кінці.
  var tries = 0, pushed = false;
  if (empty) empty.hidden = true;
  (function wait(){
    var doc = null;
    try { doc = frame.contentDocument; } catch (_e) { doc = null; }
    var ready = doc && doc.readyState === "complete" && frame.contentWindow.KM3D;
    if (ready && !pushed){ pushed = true; push(); }
    var tile = doc && doc.querySelector(".mc.has-3d");
    fit();
    if (empty) empty.hidden = !!tile || tries < 40;
    if (!tile && ++tries <= 40) setTimeout(wait, 500);
  })();
  frame.addEventListener("load", function(){ pushed = false; tries = 0; });
  if (window.ResizeObserver) new ResizeObserver(fit).observe(stage);
})();
