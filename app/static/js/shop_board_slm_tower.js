// Варіант B «Вежа»: профіль збоку. Деталі (коронки, міст, абатмент) виросли
// рівно на N/M своєї висоти — це ПРАВДА з футера. Решта висоти — ледь видимий
// силует «що ще буде». Свіжі шари гарячі (бурштин), нижче — сталь.
// Рух (ракель по верху, точка лазера по верхньому шару, пульс лінії шару) —
// ілюстрація; швидкість циклу нічого не стверджує.
// Статика (камера, ложе, деталі з градієнтом) — один офскрін на (розмір, шар).
//
// Підключається ЛИШЕ коли в Налаштуваннях обрано цей стиль: `shop_board_slm.js`
// сам додає цей файл, побачивши `data-style` на плитці, і далі кличе
// `KMSlm.<стиль>.draw(ctx, w, h, s)` на ТОМУ САМОМУ полотні `#slm` (розмір,
// футер, перенос полотна при свапі — ті самі, що в «як зараз»). Стан `s`:
//   t        — час ілюстрації; іде лише поки машина друкує (і без reduced-motion);
//   doneT    — null під час друку, інакше секунди від зупинки (99 — давно/статично);
//   still    — prefers-reduced-motion: один статичний кадр;
//   finished — плитка в стані «завершено» (`t-done`): тоді й лише тоді «ГОТОВО»,
//              і воно СТОЇТЬ, поки друк завершений (як «ЗНЯТИ» на верстатах);
//   layer, total — числа з футера (шар N з M), єдина правда на полотні.
(function () {
  "use strict";
  var RECOAT = 0.7, SCAN = 2.8, CYCLE = RECOAT + SCAN, OFFSET = 0.9;

  // Деталі: центр по ширині камери (0..1), ширина (частка камери), повна висота
  // (частка висоти друку), форма. v — 0..1 по висоті деталі, 0..0.12 — підтримки.
  var SUP = 0.12;
  function crownHW(v) {
    if (v < SUP) return 0;
    var k = (v - SUP) / (1 - SUP);
    var hw = 0.40 + 0.10 * Math.sin(Math.min(1, k / 0.6) * Math.PI / 2);
    if (k > 0.72) hw *= Math.sqrt(Math.max(0, 1 - Math.pow((k - 0.72) / 0.28, 2)));
    return hw;
  }
  // Коронка збоку: вужча шийка, найширше на екваторі, жувальна поверхня з горбками.
  function crownPoly(cx, pw, ph, plat, v0) {
    var L = [], R = [], top = [];
    var vb = v0 === undefined ? SUP : v0;
    for (var i = 0; i <= 24; i++) {
      var k = i / 24, v = vb + (0.86 - vb) * k;
      var hw = pw / 2 * (0.78 + 0.22 * Math.sin(Math.min(1, k / 0.7) * Math.PI / 2) - (k > 0.85 ? (k - 0.85) * 0.9 : 0));
      L.push([cx - hw, plat - v * ph]); R.unshift([cx + hw, plat - v * ph]);
    }
    var hwTop = pw / 2 * 0.865;
    for (var j = 0; j <= 24; j++) {
      var u = j / 24;                               // зліва направо по жувальній поверхні
      var x = cx - hwTop + 2 * hwTop * u;
      var shoulder = Math.sin(u * Math.PI);         // заокруглені краї
      var cusp = 0.5 - 0.5 * Math.cos(u * Math.PI * 4); // два горбки, западина між ними
      var v = 0.86 + Math.pow(shoulder, 0.6) * (0.05 + 0.07 * cusp);
      top.push([x, plat - v * ph]);
    }
    return L.concat(top, R);
  }
  function abutHW(v) {
    if (v < SUP) return 0;
    var k = (v - SUP) / (1 - SUP);
    if (k < 0.32) return 0.30;
    if (k < 0.42) return 0.30 + (k - 0.32) / 0.10 * 0.20;
    if (k < 0.50) return 0.50;
    var t = (k - 0.50) / 0.50;
    return 0.34 - 0.10 * t - (t > 0.92 ? (t - 0.92) / 0.08 * 0.06 : 0);
  }
  var PARTS = [
    { x: 0.135, w: 0.150, h: 0.64, crown: true },
    { x: 0.290, w: 0.105, h: 0.58, crown: true },
    { x: 0.505, w: 0.270, h: 0.62, bridge: true },
    { x: 0.715, w: 0.085, h: 0.84, f: abutHW },
    { x: 0.865, w: 0.140, h: 0.70, crown: true }
  ];

  function geom(w, h) {
    var plat = h * 0.86, top = h * 0.11;
    return { w: w, h: h, x0: w * 0.035, x1: w * 0.965, plat: plat, H: plat - top, u: h / 324 };
  }
  // Полігони деталі в пікселях (міст — кілька полігонів, заповнюються як обʼєднання).
  function polys(g, p) {
    var cw = g.x1 - g.x0, cx = g.x0 + p.x * cw, pw = p.w * cw, ph = p.h * g.H;
    function prof(f, ox, ww, hh, v0) {
      var L = [], R = [];
      for (var i = 0; i <= 40; i++) {
        var v = (v0 || SUP) + (1 - (v0 || SUP)) * i / 40;
        var hw = f(v) * ww;
        L.push([ox - hw, g.plat - v * hh]); R.unshift([ox + hw, g.plat - v * hh]);
      }
      return L.concat(R);
    }
    if (p.crown) return [crownPoly(cx, pw, ph, g.plat)];
    if (!p.bridge) return [prof(p.f, cx, pw, ph)];
    var ab = pw * 0.34;
    // міст: дві опорні коронки + проміжна, що висить вище (під нею підтримки)
    var out = [crownPoly(cx - pw * 0.33, ab, ph, g.plat), crownPoly(cx + pw * 0.33, ab, ph, g.plat),
               crownPoly(cx, ab * 1.02, ph, g.plat, 0.34)];
    // перемички між коронками
    out.push([[cx - pw * 0.30, g.plat - ph * 0.52], [cx + pw * 0.30, g.plat - ph * 0.52],
              [cx + pw * 0.30, g.plat - ph * 0.74], [cx - pw * 0.30, g.plat - ph * 0.74]]);
    return out;
  }
  // Кожен полігон — окремим fill: інакше перекриття з різним обходом дають дірки.
  function trace(ctx, list) {
    list.forEach(function (poly) {
      ctx.beginPath();
      poly.forEach(function (q, i) { if (i) ctx.lineTo(q[0], q[1]); else ctx.moveTo(q[0], q[1]); });
      ctx.closePath();
      ctx.fill();
    });
  }
  // Підтримки — тонкі стійки від платформи до низу тіла.
  function supports(ctx, g, p, topY) {
    var cw = g.x1 - g.x0, cx = g.x0 + p.x * cw, pw = p.w * cw, ph = p.h * g.H;
    var spans = p.bridge ? [[cx - pw * 0.5, cx + pw * 0.5, SUP], [cx - pw * 0.14, cx + pw * 0.14, 0.34]]
                         : [[cx - pw * 0.40, cx + pw * 0.40, SUP]];
    ctx.lineWidth = Math.max(1, 1.2 * g.u);
    spans.forEach(function (s) {
      var yTop = Math.max(topY, g.plat - s[2] * ph);
      for (var x = s[0] + 3 * g.u; x < s[1] - 2 * g.u; x += 6 * g.u) {
        ctx.beginPath(); ctx.moveTo(x, g.plat); ctx.lineTo(x, yTop); ctx.stroke();
      }
    });
  }

  function mk(w, h) { var c = document.createElement("canvas"); c.width = w; c.height = h; return c; }
  function rnd(seed) { return function () { seed = (seed * 16807) % 2147483647; return seed / 2147483647; }; }

  var cache = { key: "" };
  function build(w, h, frac) {
    var g = geom(w, h);
    var topY = g.plat - frac * g.H;
    var c = mk(w, h), x = c.getContext("2d");
    // камера
    var bg = x.createLinearGradient(0, 0, 0, h);
    bg.addColorStop(0, "#0b0805"); bg.addColorStop(1, "#15100a");
    x.fillStyle = bg; x.fillRect(0, 0, w, h);
    // порошкове ложе до поточного шару (розріз)
    var bed = x.createLinearGradient(0, topY, 0, g.plat);
    bed.addColorStop(0, "#3b352c"); bed.addColorStop(1, "#28231c");
    x.fillStyle = bed; x.fillRect(g.x0, topY, g.x1 - g.x0, g.plat - topY);
    var r = rnd(11), n = Math.round((g.x1 - g.x0) * (g.plat - topY) * 0.035);
    for (var i = 0; i < n; i++) {
      x.fillStyle = r() > 0.5 ? "rgba(255,240,215,.07)" : "rgba(0,0,0,.18)";
      x.fillRect(g.x0 + r() * (g.x1 - g.x0), topY + r() * (g.plat - topY), 1.2, 1.2);
    }
    // силует «що ще буде» — ледь видно
    var gh = mk(w, h), gx = gh.getContext("2d");
    gx.fillStyle = "#ffd894";
    PARTS.forEach(function (p) { trace(gx, polys(g, p)); });
    x.globalAlpha = 0.06; x.drawImage(gh, 0, 0); x.globalAlpha = 1;
    // збудоване: сталь, внизу темніша; верх — у гарячому шарі (окремо)
    x.save();
    x.beginPath(); x.rect(0, topY, w, h - topY); x.clip();
    var steel = x.createLinearGradient(0, topY, 0, g.plat);
    steel.addColorStop(0, "#b9c2c9"); steel.addColorStop(0.5, "#8d98a1"); steel.addColorStop(1, "#5e6870");
    x.fillStyle = steel;
    PARTS.forEach(function (p) { trace(x, polys(g, p)); });
    // шари — ледь помітні горизонтальні риски по металу
    x.globalCompositeOperation = "source-atop";
    x.fillStyle = "rgba(0,0,0,.10)";
    for (var y = g.plat; y > topY; y -= Math.max(3, 3.2 * g.u)) x.fillRect(0, y, w, 1);
    x.globalCompositeOperation = "source-over";
    x.strokeStyle = "#6c767e";
    PARTS.forEach(function (p) { supports(x, g, p, topY); });
    x.restore();
    // платформа
    var pl = x.createLinearGradient(0, g.plat, 0, g.plat + h * 0.07);
    pl.addColorStop(0, "#7b8790"); pl.addColorStop(0.15, "#3f4950"); pl.addColorStop(1, "#1b2228");
    x.fillStyle = pl; x.fillRect(g.x0 - 4 * g.u, g.plat, g.x1 - g.x0 + 8 * g.u, h * 0.07);
    // стінки камери
    x.fillStyle = "#3a4249";
    x.fillRect(g.x0 - 6 * g.u, h * 0.06, 2.5 * g.u, h);
    x.fillRect(g.x1 + 3.5 * g.u, h * 0.06, 2.5 * g.u, h);

    // гарячий шар: окремо, щоб остигання при завершенні було лише альфою
    var hot = mk(w, h), hx = hot.getContext("2d");
    var band = Math.max(14 * g.u, g.H * 0.16);
    hx.save();
    hx.beginPath(); hx.rect(0, topY, w, band); hx.clip();
    var hg = hx.createLinearGradient(0, topY, 0, topY + band);
    hg.addColorStop(0, "rgba(255,190,100,1)"); hg.addColorStop(0.25, "rgba(255,150,60,.85)");
    hg.addColorStop(0.6, "rgba(190,110,50,.40)"); hg.addColorStop(1, "rgba(150,100,60,0)");
    hx.fillStyle = "#fff";
    PARTS.forEach(function (p) { trace(hx, polys(g, p)); });   // обʼєднання деталей...
    hx.globalCompositeOperation = "source-in";                  // ...і лише в ньому градієнт
    hx.fillStyle = hg; hx.fillRect(0, topY, w, band);
    hx.restore();

    // відрізки верхнього шару, які сканує лазер (перетин деталей з лінією topY)
    var spans = [];
    var probe = mk(w, 3).getContext("2d");
    probe.translate(0, -topY + 1);
    PARTS.forEach(function (p) { probe.fillStyle = "#fff"; trace(probe, polys(g, p)); });
    var row = probe.getImageData(0, 1, w, 1).data, inside = false, s0 = 0;
    for (var px = 0; px <= w; px++) {
      var on = px < w && row[px * 4 + 3] > 100;
      if (on && !inside) { s0 = px; inside = true; }
      if (!on && inside) { spans.push([s0, px]); inside = false; }
    }
    return { g: g, topY: topY, stat: c, hot: hot, spans: spans, total: spans.reduce(function (a, s) { return a + s[1] - s[0]; }, 0) };
  }

  function label(ctx, w, h, a) {
    if (a <= 0) return;
    var fs = Math.round(h * 0.12);
    ctx.save();
    ctx.globalAlpha = a;
    ctx.font = "700 " + fs + "px 'Golos Text', sans-serif";
    var tw = ctx.measureText("ГОТОВО").width, bw = tw + fs * 1.1, bh = fs * 1.55;
    var cx = w / 2, cy = h * 0.115;
    ctx.fillStyle = "rgba(11,8,5,.80)"; ctx.fillRect(cx - bw / 2, cy - bh / 2, bw, bh);
    ctx.strokeStyle = "rgba(255,216,148,.55)"; ctx.lineWidth = 2;
    ctx.strokeRect(cx - bw / 2 + 1, cy - bh / 2 + 1, bw - 2, bh - 2);
    ctx.fillStyle = "#ffd894"; ctx.textAlign = "center"; ctx.textBaseline = "middle";
    ctx.fillText("ГОТОВО", cx, cy + fs * 0.04);
    ctx.restore();
  }

  (window.KMSlm = window.KMSlm || {}).tower = { draw: function (ctx, w, h, s) {
    var frac = s.total ? Math.max(0, Math.min(1, s.layer / s.total)) : 0;
    var key = w + "x" + h + ":" + frac;
    if (cache.key !== key) { cache = build(w, h, frac); cache.key = key; }
    var g = cache.g, topY = cache.topY;
    ctx.drawImage(cache.stat, 0, 0);

    if (s.doneT !== null || s.still) {
      var cool = s.doneT !== null ? Math.max(0, 1 - s.doneT / 2.4) : 0.6;
      if (cool > 0) { ctx.globalAlpha = cool; ctx.drawImage(cache.hot, 0, 0); ctx.globalAlpha = 1; }
      if (s.doneT !== null && s.finished) label(ctx, w, h, Math.min(1, (s.doneT - 0.5) / 0.5));
      return;
    }
    ctx.drawImage(cache.hot, 0, 0);

    var tt = s.t + OFFSET, ph = tt % CYCLE;
    // лінія поточного шару: тонка, пульсує
    var pulse = 0.45 + 0.35 * (0.5 + 0.5 * Math.sin(s.t * Math.PI * 2 / 1.6));
    ctx.fillStyle = "rgba(255,216,148," + pulse.toFixed(3) + ")";
    ctx.fillRect(g.x0, Math.round(topY) - 1, g.x1 - g.x0, Math.max(1, 1.2 * g.u));
    // номер шару біля лінії — те саме число, що у футері
    ctx.font = "600 " + Math.round(13 * g.u) + "px 'JetBrains Mono', monospace";
    ctx.textAlign = "right"; ctx.textBaseline = "bottom";
    ctx.fillStyle = "rgba(255,216,148,.75)";
    ctx.fillText(String(s.layer), g.x1 - 4 * g.u, topY - 4 * g.u);

    if (ph < RECOAT) {
      var k = ph / RECOAT; k = k * k * (3 - 2 * k);
      var rx = g.x0 + (g.x1 - g.x0) * k;
      ctx.fillStyle = "rgba(150,138,118,.8)";                     // свіжий шар позаду ракеля
      ctx.fillRect(g.x0, topY - 2 * g.u, rx - g.x0, 2 * g.u);
      ctx.fillStyle = "#7d8a95";
      ctx.fillRect(rx - 3 * g.u, topY - 22 * g.u, 6 * g.u, 20 * g.u);
      ctx.fillStyle = "#c9d2d8";
      ctx.fillRect(rx + 2 * g.u, topY - 22 * g.u, 1.2 * g.u, 20 * g.u);
      return;
    }
    // лазер біжить уздовж верхнього шару по перетинах деталей
    var sk = Math.min(1, (ph - RECOAT) / (SCAN * 0.96));
    var pos = sk * cache.total, acc = 0, hx = null;
    for (var i = 0; i < cache.spans.length; i++) {
      var sp = cache.spans[i], L = sp[1] - sp[0];
      var done = Math.min(L, Math.max(0, pos - acc));
      if (done > 0) {
        ctx.fillStyle = "rgba(255,246,224,.95)";
        ctx.fillRect(sp[0], topY - 1.5 * g.u, done, 3 * g.u);
      }
      if (hx === null && pos <= acc + L) hx = sp[0] + done;
      acc += L;
    }
    if (hx !== null && sk < 1) {
      ctx.strokeStyle = "rgba(255,220,160,.35)"; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(hx, 0); ctx.lineTo(hx, topY); ctx.stroke();
      ctx.fillStyle = "#ffb454";
      ctx.beginPath(); ctx.arc(hx, topY, 4.2 * g.u, 0, Math.PI * 2); ctx.fill();
      ctx.fillStyle = "#fffaf0";
      ctx.beginPath(); ctx.arc(hx, topY, 2.4 * g.u, 0, Math.PI * 2); ctx.fill();
    }
  } };
})();
