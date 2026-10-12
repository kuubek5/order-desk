// Рядок програми біля диска на табло цеху — ОДНИМ рядком (власник 12.10.26:
// «більшість рядків не влазить в один рядок повністю — нехай повільно
// рухаються одним рядком»).
//
// Влазить — стоїть, як є. Не влазить — повільно їде: пауза на початку, плавно
// до кінця (хвіст Sum3D тоді видно цілим), пауза, плавно назад. Ніколи не
// «проскакує» на початок і не показує напівпорожню рамку: зсув рівно настільки,
// наскільки текст довший за рамку. Рух — лише `transform` (композитор), фаза —
// від годинника, тож свап шматка раз на 30 с рух не перезапускає.
//
// prefers-reduced-motion — нічого не робимо: CSS лишає перенос на кілька
// рядків, як до 12.10.26 (нічого не урізається ніколи).
(function (root) {
  "use strict";

  var SPEED = 30;      // px/с — повільно, щоб читалось з проходу
  var PAUSE = 2000;    // мс на кожному кінці

  // Чиста функція (її тестує node): чи треба їхати і як саме.
  // null — влазить; інакше зсув `d` (px), період і ключові кадри.
  function plan(boxW, textW) {
    if (!(boxW > 0) || !(textW > 0) || textW <= boxW + 0.5) return null;
    var d = Math.ceil(textW - boxW);
    var travel = d / SPEED * 1000;
    var period = Math.round(2 * PAUSE + 2 * travel);
    var a = PAUSE / period, b = (PAUSE + travel) / period, c = (2 * PAUSE + travel) / period;
    var to = "translateX(" + (-d) + "px)";
    return {
      d: d,
      period: period,
      keyframes: [
        { offset: 0, transform: "translateX(0px)" },
        { offset: a, transform: "translateX(0px)", easing: "ease-in-out" },
        { offset: b, transform: to },
        { offset: c, transform: to, easing: "ease-in-out" },
        { offset: 1, transform: "translateX(0px)" }
      ]
    };
  }

  function reduced() {
    return !!(root.matchMedia && root.matchMedia("(prefers-reduced-motion: reduce)").matches);
  }

  // Поміряти кожну рамку й (пере)запустити рух. Кличеться після першого
  // малюнка, після кожного свапу, коли довантажився шрифт і на зміну розміру.
  function apply(doc, now) {
    if (!doc || reduced()) return;
    var t = now === undefined ? Date.now() : now;
    var boxes = doc.querySelectorAll(".mc-id .prog");
    for (var i = 0; i < boxes.length; i++) {
      var box = boxes[i];
      var track = box.querySelector(".pt");
      if (!track) continue;
      if (track._kmAnim) { track._kmAnim.cancel(); track._kmAnim = null; }
      var p = plan(box.clientWidth, track.scrollWidth);
      if (!p) {
        box.classList.remove("is-run");
        box.style.removeProperty("--prog-d");
        continue;
      }
      box.classList.add("is-run");
      box.style.setProperty("--prog-d", p.d + "px");
      if (typeof track.animate !== "function") continue;
      var anim = track.animate(p.keyframes, { duration: p.period, iterations: Infinity });
      anim.currentTime = t % p.period;
      track._kmAnim = anim;
    }
  }

  var api = { plan: plan, apply: apply, SPEED: SPEED, PAUSE: PAUSE };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  root.KMProg = api;

  var doc = root.document;
  if (!doc || !doc.addEventListener) return;
  function run() { apply(doc); }
  if (doc.readyState === "loading") doc.addEventListener("DOMContentLoaded", run);
  else run();
  if (doc.fonts && doc.fonts.ready) doc.fonts.ready.then(run);
  var resizeTimer = 0;
  root.addEventListener("resize", function () {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(run, 200);
  });
})(typeof window !== "undefined" ? window : globalThis);
