// SLM-друк у розрізі — плитка SISMA на табло цеху (/t/<token>/shop).
//
// Що тут ПРАВДА і що ІЛЮСТРАЦІЯ. Правда — висота спеченого блоку: вона йде
// з `layer / layers_total`, тобто з того самого числа, яке читається з екрана
// машини. Ілюстрація — хід ракеля, опускання платформи й сканування лазером:
// цих фаз машина не повідомляє, тому цикл крутиться сам по собі й ніде не
// підписаний як стан. Видавати намальовану фазу за телеметрію не можна.
//
// Геометрія параметрична від розміру полотна: та сама функція малює і
// мініатюру в комірці сітки, і велику плитку на 4K.
(function () {
  "use strict";

  var canvas = document.getElementById("slm");
  if (!canvas || !canvas.getContext) return;

  var PHASES = [["dip", 0.65], ["feed", 0.55], ["sweep", 2.0], ["back", 1.0], ["laser", 2.4]];
  var phase = 0;
  var phaseT = 0;
  var last = 0;
  var DPR = 1.5;

  // Зерно порошку генерується ОДИН раз: інакше воно «кипить» на кожному кадрі.
  // І малюється теж один раз — у власний шар (`grainLayer`): 1400 крапок під
  // `clip()` шість разів на кадр коштували на цій машині 0.38 мс, а на міні-ПК
  // біля телевізора відсікання — найдорожча операція canvas, яка там і є.
  var GRAIN = [];
  for (var i = 0; i < 1400; i++) {
    GRAIN.push({
      x: Math.random(), y: Math.random(),
      r: Math.random() * 0.55 + 0.3, a: Math.random() * 0.5 + 0.2
    });
  }
  var SPARK = [];
  for (var j = 0; j < 12; j++) {
    SPARK.push({ a: Math.random() * Math.PI * 2, v: Math.random() * 0.6 + 0.4, s: Math.random() });
  }

  // Скільки надруковано. Число живе у футері плитки, який оновлює сервер, —
  // малювання читає його звідти, щоб не тримати другої копії правди.
  function progress() {
    var tile = document.querySelector(".mc.sis");
    if (!tile) return 0;
    var lay = tile.querySelector(".sis-foot .lay");
    var of = tile.querySelector(".sis-foot .of");
    if (!lay || !of) return 0;
    var layer = parseInt(lay.textContent, 10);
    var total = parseInt(String(of.textContent).replace(/\D+/g, ""), 10);
    if (!layer || !total) return 0;
    return Math.max(0, Math.min(1, layer / total));
  }

  // Шар зерна перемальовується лише коли змінився розмір полотна: `u`
  // однозначно виводиться з (w, h), тож ключа з двох чисел досить.
  var GRAIN_CV = null, grainW = 0, grainH = 0;
  function grainLayer(w, h, u) {
    if (GRAIN_CV && grainW === w && grainH === h) return GRAIN_CV;
    GRAIN_CV = GRAIN_CV || document.createElement("canvas");
    GRAIN_CV.width = w; GRAIN_CV.height = h;
    grainW = w; grainH = h;
    var g2 = GRAIN_CV.getContext("2d");
    g2.clearRect(0, 0, w, h);
    for (var i = 0; i < GRAIN.length; i++) {
      var g = GRAIN[i];
      g2.globalAlpha = g.a;
      g2.fillStyle = g.r > 0.6 ? "#ffffff" : "#000000";
      g2.fillRect(g.x * w, g.y * h, g.r * 5 * u + 1, g.r * 5 * u + 1);
    }
    g2.globalAlpha = 1;
    return GRAIN_CV;
  }

  function draw(ctx, w, h) {
    var ph = PHASES[phase][0];
    var k = phaseT / PHASES[phase][1];
    var done = progress();

    // Глибину обмежує і ширина: у вузькій комірці колодязі інакше
    // вироджуються в щілини. Залишок висоти центруємо.
    var BUILD_D = Math.min(h * 0.68, w * 0.86);
    var SIDE_D = BUILD_D * 0.58;
    var u = BUILD_D / 620;
    var FLOOR = Math.min(h * 0.25, w * 0.22);
    var offY = Math.max(0, (h - (FLOOR + BUILD_D + 14 * u)) / 2);
    FLOOR += offY;
    var SIDE_B = FLOOR + SIDE_D;
    var FEED = { x: w * 0.055, w: w * 0.165, d: SIDE_D };
    var BUILD = { x: w * 0.315, w: w * 0.370, d: BUILD_D };
    var OVER = { x: w * 0.780, w: w * 0.165, d: SIDE_D };

    // Шар зерна той самий для всього полотна, тож із нього просто беруть
    // потрібний прямокутник. Прозорість крапки вже впечена в шар, а `alpha`
    // множить її зверху — картинка та сама, що й при поштучному малюванні.
    var layer = grainLayer(w, h, u);
    function grain(x, y, ww, hh, alpha) {
      var sx = Math.max(0, x), sy = Math.max(0, y);
      var sw = Math.min(w, x + ww) - sx, sh = Math.min(h, y + hh) - sy;
      if (sw <= 0.5 || sh <= 0.5) return;
      ctx.globalAlpha = alpha;
      ctx.drawImage(layer, sx, sy, sw, sh, sx, sy, sw, sh);
      ctx.globalAlpha = 1;
    }
    // Порошок теплий, пісочний — щоб холодний сталевий метал не зливався з ним.
    function powder(x, y, ww, hh, lit) {
      if (hh <= 0.5 || ww <= 0.5) return;
      var g = ctx.createLinearGradient(x, y, x, y + hh);
      g.addColorStop(0, lit ? "#bfae87" : "#a5966f");
      g.addColorStop(1, lit ? "#93855f" : "#6f6349");
      ctx.fillStyle = g; ctx.fillRect(x, y, ww, hh);
      grain(x, y, ww, hh, 0.55);
      var sh = ctx.createLinearGradient(x, 0, x + ww, 0);
      sh.addColorStop(0, "rgba(0,0,0,.34)"); sh.addColorStop(0.12, "rgba(0,0,0,0)");
      sh.addColorStop(0.88, "rgba(0,0,0,0)"); sh.addColorStop(1, "rgba(0,0,0,.34)");
      ctx.fillStyle = sh; ctx.fillRect(x, y, ww, hh);
    }
    // Порожня частина колодязя — освітлена стінка циліндра, а не чорна діра.
    function well(o) {
      var ig = ctx.createLinearGradient(o.x, 0, o.x + o.w, 0);
      ig.addColorStop(0, "#222d37"); ig.addColorStop(0.18, "#0d1319");
      ig.addColorStop(0.82, "#0d1319"); ig.addColorStop(1, "#222d37");
      ctx.fillStyle = ig; ctx.fillRect(o.x, FLOOR, o.w, o.d);
      var wall = Math.max(2.5, 6 * u);
      ctx.strokeStyle = "#46545f"; ctx.lineWidth = wall;
      ctx.beginPath();
      ctx.moveTo(o.x - wall / 2, FLOOR); ctx.lineTo(o.x - wall / 2, FLOOR + o.d);
      ctx.lineTo(o.x + o.w + wall / 2, FLOOR + o.d); ctx.lineTo(o.x + o.w + wall / 2, FLOOR);
      ctx.stroke();
    }
    // Поршень — суцільний блок до дна: штока всередині колодязя не видно.
    function piston(o, topY) {
      var b = FLOOR + o.d;
      var bg = ctx.createLinearGradient(o.x, 0, o.x + o.w, 0);
      bg.addColorStop(0, "#1e2832"); bg.addColorStop(0.32, "#2e3a44");
      bg.addColorStop(0.75, "#1a232b"); bg.addColorStop(1, "#12191f");
      ctx.fillStyle = bg; ctx.fillRect(o.x, topY, o.w, b - topY);
      var g = ctx.createLinearGradient(0, topY, 0, topY + 26 * u);
      g.addColorStop(0, "#8496a3"); g.addColorStop(1, "#3a4854");
      ctx.fillStyle = g; ctx.fillRect(o.x, topY, o.w, Math.max(7, 22 * u));
      ctx.fillStyle = "#111922";
      ctx.fillRect(o.x, topY + 22 * u, o.w, Math.max(1.5, 3 * u));
    }

    // Верх шару тримається біля поверхні — платформа ОПУСКАЄТЬСЯ в міру друку.
    var partTop = FLOOR + 6 * u;
    if (ph === "dip") partTop += k * 8 * u;
    var full = BUILD_D - 64 * u;
    var grown = done * full;
    var platTop = partTop + grown;

    var feedLow = SIDE_B - 34 * u, feedHigh = FLOOR + 20 * u;
    var feedTop = feedLow - done * (feedLow - feedHigh);
    if (ph === "feed") feedTop -= k * 10 * u;

    var bg2 = ctx.createLinearGradient(0, 0, 0, h);
    bg2.addColorStop(0, "#141b22"); bg2.addColorStop(1, "#0b1015");
    ctx.fillStyle = bg2; ctx.fillRect(0, 0, w, h);
    var plate = ctx.createLinearGradient(0, FLOOR - 22 * u, 0, FLOOR);
    plate.addColorStop(0, "#2c3742"); plate.addColorStop(1, "#161e26");
    ctx.fillStyle = plate; ctx.fillRect(0, FLOOR - 22 * u, w, 22 * u);
    ctx.fillStyle = "#55636f"; ctx.fillRect(0, FLOOR - 22 * u, w, Math.max(1.5, 2.5 * u));

    well(FEED); well(BUILD); well(OVER);

    piston(FEED, feedTop);
    powder(FEED.x, FLOOR, FEED.w, feedTop - FLOOR, false);
    if (ph === "feed" || ph === "sweep") {
      var hump = (ph === "feed" ? k : 1 - Math.min(1, k * 1.6)) * 15 * u;
      powder(FEED.x, FLOOR - hump, FEED.w, hump, true);
    }

    piston(BUILD, platTop);
    var swept = ph === "sweep" ? Math.min(1, k * 1.12) : (ph === "dip" || ph === "feed" ? 0 : 1);
    // Порошок лягає ДВОМА смугами обабіч блоку: під нього матеріал не заходить.
    var ax = BUILD.x + BUILD.w * 0.09, aw = BUILD.w * 0.82;
    var gapL = ax - BUILD.x, gapR = BUILD.x + BUILD.w - (ax + aw);
    var fillTo = BUILD.x + BUILD.w * swept;
    powder(BUILD.x, FLOOR, Math.min(gapL, fillTo - BUILD.x), platTop - FLOOR, false);
    powder(ax + aw, FLOOR, Math.min(gapR, Math.max(0, fillTo - (ax + aw))), platTop - FLOOR, false);

    if (grown > 0) {
      var mg = ctx.createLinearGradient(ax, 0, ax + aw, 0);
      mg.addColorStop(0, "#49545e"); mg.addColorStop(0.20, "#d5dee5");
      mg.addColorStop(0.52, "#8d9aa5"); mg.addColorStop(0.78, "#bcc7ce");
      mg.addColorStop(1, "#39434b");
      ctx.fillStyle = mg; ctx.fillRect(ax, partTop, aw, grown);
      ctx.fillStyle = "rgba(0,0,0,.075)";
      for (var y = partTop + 4; y < platTop; y += Math.max(4, 10 * u)) ctx.fillRect(ax, y, aw, 1);
      var hot = ctx.createLinearGradient(0, partTop - 2 * u, 0, partTop + 20 * u);
      hot.addColorStop(0, "rgba(255,224,170,.70)");
      hot.addColorStop(0.35, "rgba(255,176,46,.30)");
      hot.addColorStop(1, "rgba(255,176,46,0)");
      ctx.fillStyle = hot; ctx.fillRect(ax, partTop - 2 * u, aw, 22 * u);
      ctx.fillStyle = "rgba(255,240,210,.9)";
      ctx.fillRect(ax, partTop, aw, Math.max(1.5, 3 * u));
    }

    var overFill = 22 * u + done * (SIDE_D - 58 * u);
    var heapTop = SIDE_B - overFill;
    powder(OVER.x, heapTop, OVER.w, overFill, false);
    if (ph === "sweep" && k > 0.78) {
      var fall = Math.min(1, (k - 0.78) / 0.15);
      powder(OVER.x + OVER.w * 0.30, FLOOR, OVER.w * 0.26, (heapTop - FLOOR) * fall, true);
    }

    var x0 = Math.max(34 * u, FEED.x - w * 0.035);
    var x1 = Math.min(w - 34 * u, OVER.x + OVER.w + w * 0.012);
    var rx = ph === "sweep" ? x0 + (x1 - x0) * k : (ph === "back" ? x1 - (x1 - x0) * k : x0);
    if (ph === "sweep" && k < 0.84) {
      var roll = 20 * u * (1 - k * 0.55);
      ctx.fillStyle = "#b4ab99";
      ctx.beginPath();
      ctx.ellipse(rx + 30 * u, FLOOR - roll / 2, 38 * u, roll / 2, 0, 0, Math.PI * 2);
      ctx.fill();
    }
    var bh = Math.min(offY + FLOOR - 4, 104 * u);
    var hg = ctx.createLinearGradient(rx - 30 * u, 0, rx + 30 * u, 0);
    hg.addColorStop(0, "#2c3b49"); hg.addColorStop(0.4, "#5b6e7e"); hg.addColorStop(1, "#22303c");
    ctx.fillStyle = hg; ctx.fillRect(rx - 30 * u, FLOOR - bh, 60 * u, bh - 12 * u);
    ctx.fillStyle = "#98aebd"; ctx.fillRect(rx - 7 * u, FLOOR - 14 * u, 14 * u, 14 * u);

    if (ph === "laser") {
      // П'ять проходів за фазу. Уповільнення вдвічі пробували 15.09.26 —
      // власник повернув швидкість назад: повільний промінь читається як
      // млява машина, а це головна ознака, що принтер працює.
      var hatch = 0.5 - 0.5 * Math.cos(k * Math.PI * 10);
      var lx = BUILD.x + BUILD.w * (0.06 + hatch * 0.88);
      ctx.save();
      ctx.globalCompositeOperation = "lighter";   // світло додається, а не закриває
      var beam = ctx.createLinearGradient(lx, 0, lx, partTop);
      beam.addColorStop(0, "rgba(255,196,104,0)");
      beam.addColorStop(0.45, "rgba(255,196,104,.30)");
      beam.addColorStop(1, "rgba(255,232,190,.95)");
      ctx.fillStyle = beam;
      ctx.beginPath();
      ctx.moveTo(lx - 26 * u, 0); ctx.lineTo(lx + 26 * u, 0);
      ctx.lineTo(lx + 4 * u, partTop); ctx.lineTo(lx - 4 * u, partTop);
      ctx.closePath(); ctx.fill();
      var rad = Math.max(22, 92 * u);
      var gl = ctx.createRadialGradient(lx, partTop, 0, lx, partTop, rad);
      gl.addColorStop(0, "rgba(255,205,120,.95)");
      gl.addColorStop(0.3, "rgba(255,160,50,.45)");
      gl.addColorStop(1, "rgba(255,140,30,0)");
      ctx.fillStyle = gl;
      ctx.beginPath(); ctx.ellipse(lx, partTop, rad, rad * 0.62, 0, 0, Math.PI * 2); ctx.fill();
      ctx.fillStyle = "#fffaf0";
      ctx.beginPath();
      ctx.ellipse(lx, partTop, Math.max(3.5, 11 * u), Math.max(2, 6 * u), 0, 0, Math.PI * 2);
      ctx.fill();
      SPARK.forEach(function (s, i) {
        var t = (k * 3 + s.s) % 1;
        var d = t * 34 * u * s.v;
        var a = s.a + k * 2;
        ctx.globalAlpha = (1 - t) * 0.85;
        ctx.fillStyle = i % 3 ? "#ffd28a" : "#fff1d6";
        ctx.fillRect(lx + Math.cos(a) * d, partTop + Math.sin(a) * d * 0.55,
          Math.max(1, 2.2 * u), Math.max(1, 2.2 * u));
      });
      ctx.globalAlpha = 1;
      ctx.restore();
    }
  }

  // Повертає true, лише якщо справді намалювало. Це не дрібниця: поки плитка
  // не отримала висоту (перший кадр після свапу, вузька комірка), малювати
  // нема на чому, і зупинений друк інакше запамʼятав би ПОРОЖНЄ полотно як
  // намальоване й не перемалював би його ніколи.
  function paint() {
    // Полотно переносять між оновленнями, тому щоразу шукаємо його наново.
    var cv = document.getElementById("slm");
    if (!cv) return false;
    var w = Math.round(cv.clientWidth * DPR), h = Math.round(cv.clientHeight * DPR);
    if (w < 8 || h < 8) return false;
    if (cv.width !== w || cv.height !== h) { cv.width = w; cv.height = h; }
    draw(cv.getContext("2d"), w, h);
    return true;
  }

  var still = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // Чи машина ДРУКУЄ просто зараз. Прапорець ставить сервер на плитці; читаємо
  // його щокадру, бо живий шматок табло свапається раз на 30 с і плитка щоразу
  // нова (полотно при цьому переносять, а плитку — ні).
  function printing() {
    var tile = document.querySelector(".mc.sis");
    return !!(tile && tile.dataset.printing === "1");
  }

  // Малюємо КОЖЕН кадр, який дає браузер. Обмеження в 60 кадрів/с пробували
  // 15.09.26 й прибрали: на цеховому телевізорі 60 Гц воно не давало нічого
  // (rAF там і так 60), а на швидшому екрані лише різало плавність.
  var frozenAt = "";
  function frame(ms) {
    var live = printing();
    if (live && !still && !document.hidden) {
      phaseT += Math.min(0.05, (ms - (last || ms)) / 1000);
      if (phaseT >= PHASES[phase][1]) { phaseT = 0; phase = (phase + 1) % PHASES.length; }
    }
    last = ms;
    if (live) {
      frozenAt = "";
      paint();
    } else {
      // Машина стоїть. Фазу скидаємо в початкову НЕ з косметики: застигнути
      // могло і на «laser», а нерухомий промінь над платформою читається як
      // живий друк — саме та брехня, від якої зупинка й рятує.
      phase = 0;
      phaseT = 0;
      // І перемальовуємо лише коли є що змінити: цілодобове полотно з нерухомою
      // картинкою не має гріти процесор міні-ПК біля телевізора.
      var cv = document.getElementById("slm");
      var sig = cv ? cv.clientWidth + "x" + cv.clientHeight + ":" + progress() : "";
      if (sig !== frozenAt && paint()) {
        frozenAt = sig;
      }
    }
    requestAnimationFrame(frame);
  }

  paint();
  requestAnimationFrame(frame);
})();
