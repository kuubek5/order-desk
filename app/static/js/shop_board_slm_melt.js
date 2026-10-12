// Варіант A «Розпечена ванна»: вигляд згори на платформу, як у вікно машини.
// Правда — лише числа у футері (шар N з M, %, час кінця) і прапорець друку.
// Цикл шару (ракель → штрихування) — ілюстрація, його швидкість нічого не стверджує.
// Статика (порошок, шари) малюється в офскрін-полотна раз на шар; на кадр —
// кілька drawImage, хвіст ванни (~12 відрізків) і крапка.
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
  var RECOAT = 0.9, SCAN = 3.1, CYCLE = RECOAT + SCAN, OFFSET = 1.0;
  var ROT = 67 * Math.PI / 180;

  // Перерізи деталей на рівні поточного шару, у координатах платформи (R = 1).
  function blob(cx, cy, r, seed, n) {
    var pts = [];
    for (var i = 0; i < 48; i++) {
      var a = i / 48 * Math.PI * 2;
      var k = 1 + 0.10 * Math.cos(4 * a + seed) + 0.05 * Math.cos(3 * a + seed * 2) + 0.03 * Math.sin(5 * a + seed);
      pts.push([cx + Math.cos(a) * r * k, cy + Math.sin(a) * r * k * (n || 1)]);
    }
    return [pts];
  }
  function bridge(cx, cy, len, wid, ang) {
    var pts = [], ca = Math.cos(ang), sa = Math.sin(ang);
    for (var i = 0; i < 72; i++) {
      var a = i / 72 * Math.PI * 2;
      var x = Math.cos(a), y = Math.sin(a);
      // супереліпс із двома «шийками» між опорами
      var sx = Math.sign(x) * Math.pow(Math.abs(x), 0.55) * len / 2;
      var waist = 1 - 0.28 * Math.pow(Math.cos(x * Math.PI * 1.5), 2) * (Math.abs(x) < 0.7 ? 1 : 0);
      var sy = Math.sign(y) * Math.pow(Math.abs(y), 0.75) * wid / 2 * waist;
      pts.push([cx + sx * ca - sy * sa, cy + sx * sa + sy * ca]);
    }
    return [pts];
  }
  function ring(cx, cy, r, hole) {
    var o = blob(cx, cy, r, 1.3)[0], h = [];
    for (var i = 0; i < 24; i++) {
      var a = -i / 24 * Math.PI * 2;
      h.push([cx + Math.cos(a) * hole, cy + Math.sin(a) * hole]);
    }
    return [o, h];
  }
  // Вікно ширше за висоту: платформу видно крупно, верх і низ кола за рамкою.
  var PARTS = [
    blob(-0.66, -0.26, 0.135, 0.4),
    blob(-0.22, -0.30, 0.125, 2.1),
    blob(0.22, -0.27, 0.130, 4.0),
    blob(0.64, -0.24, 0.125, 5.2),
    bridge(-0.36, 0.27, 0.70, 0.24, -0.05),
    ring(0.22, 0.29, 0.120, 0.045),
    blob(0.60, 0.27, 0.125, 3.3)
  ];

  function geom(w, h) {
    var R = Math.min(w * 0.47, h * 0.95);
    return { cx: w / 2, cy: h / 2, R: R, gap: Math.max(4, R * 0.022) };
  }
  function toPx(g, p) { return [g.cx + p[0] * g.R, g.cy + p[1] * g.R]; }

  // Штрихування одного шару: паралельні лінії під кутом, змійкою; потім контур.
  // Повертає відрізки з довжинами — по них іде ванна.
  function buildPath(g, idx) {
    var ang = 0.35 + idx * ROT, ca = Math.cos(ang), sa = Math.sin(ang);
    var segs = [], total = 0, flip = false;
    PARTS.forEach(function (part, pi) {
      var rings = part.map(function (r) { return r.map(function (p) { return toPx(g, p); }); });
      // повернути в систему, де штрихи горизонтальні
      var rot = rings.map(function (r) { return r.map(function (p) {
        var x = p[0] - g.cx, y = p[1] - g.cy; return [x * ca + y * sa, -x * sa + y * ca]; }); });
      var y0 = Infinity, y1 = -Infinity;
      rot[0].forEach(function (p) { y0 = Math.min(y0, p[1]); y1 = Math.max(y1, p[1]); });
      var start = Math.ceil(y0 / g.gap) * g.gap;
      for (var y = start; y <= y1; y += g.gap) {
        var xs = [];
        rot.forEach(function (r) {
          for (var i = 0; i < r.length; i++) {
            var a = r[i], b = r[(i + 1) % r.length];
            if ((a[1] <= y) !== (b[1] <= y)) xs.push(a[0] + (y - a[1]) / (b[1] - a[1]) * (b[0] - a[0]));
          }
        });
        xs.sort(function (p, q) { return p - q; });
        var pairs = [];
        for (var j = 0; j + 1 < xs.length; j += 2) pairs.push([xs[j], xs[j + 1]]);
        if (flip) { pairs.reverse(); pairs = pairs.map(function (p) { return [p[1], p[0]]; }); }
        flip = !flip;
        pairs.forEach(function (p) {
          var A = [g.cx + p[0] * ca - y * sa, g.cy + p[0] * sa + y * ca];
          var B = [g.cx + p[1] * ca - y * sa, g.cy + p[1] * sa + y * ca];
          var L = Math.abs(p[1] - p[0]);
          segs.push({ a: A, b: B, s0: total, len: L, part: pi });
          total += L;
        });
      }
      rings.forEach(function (r) {
        for (var i = 0; i < r.length; i++) {
          var A = r[i], B = r[(i + 1) % r.length];
          var L = Math.hypot(B[0] - A[0], B[1] - A[1]);
          segs.push({ a: A, b: B, s0: total, len: L, part: pi, contour: true });
          total += L;
        }
      });
    });
    var ends = PARTS.map(function () { return 0; });
    segs.forEach(function (s) { ends[s.part] = s.s0 + s.len; });
    return { segs: segs, total: total, ends: ends };
  }

  function partPath(ctx, g, part) {
    ctx.beginPath();
    part.forEach(function (r) {
      r.forEach(function (p, i) { var q = toPx(g, p); if (i) ctx.lineTo(q[0], q[1]); else ctx.moveTo(q[0], q[1]); });
      ctx.closePath();
    });
  }

  // ── Офскрін-шари ──
  var cache = { key: "" };
  function mk(w, h) { var c = document.createElement("canvas"); c.width = w; c.height = h; return c; }
  function rnd(seed) { return function () { seed = (seed * 16807) % 2147483647; return seed / 2147483647; }; }

  function powderLayer(w, h, g, fresh) {
    var c = mk(w, h), x = c.getContext("2d"), r = rnd(fresh ? 7 : 3);
    x.save();
    x.beginPath(); x.arc(g.cx, g.cy, g.R, 0, Math.PI * 2); x.clip();
    var gr = x.createRadialGradient(g.cx - g.R * 0.3, g.cy - g.R * 0.4, g.R * 0.1, g.cx, g.cy, g.R * 1.05);
    gr.addColorStop(0, fresh ? "#4a443a" : "#38332b");
    gr.addColorStop(1, fresh ? "#302b24" : "#221e19");
    x.fillStyle = gr; x.fillRect(0, 0, w, h);
    var n = Math.round(g.R * g.R * 0.05);
    for (var i = 0; i < n; i++) {
      var v = r();
      x.fillStyle = v > 0.5 ? "rgba(255,240,215," + (0.05 + r() * 0.10) + ")" : "rgba(0,0,0," + (0.10 + r() * 0.18) + ")";
      var s = 0.8 + r() * 1.4;
      x.fillRect(g.cx + (r() * 2 - 1) * g.R, g.cy + (r() * 2 - 1) * g.R, s, s);
    }
    x.restore();
    return c;
  }
  function baseLayer(w, h, g) {
    var c = mk(w, h), x = c.getContext("2d");
    x.fillStyle = "#0b0805"; x.fillRect(0, 0, w, h);
    // камера: темна підлога з ледь теплим відсвітом, кільце платформи
    var fl = x.createRadialGradient(g.cx, g.cy, g.R, g.cx, g.cy, g.R * 1.5);
    fl.addColorStop(0, "#1a150e"); fl.addColorStop(1, "#0b0805");
    x.fillStyle = fl; x.fillRect(0, 0, w, h);
    x.lineWidth = Math.max(3, g.R * 0.03);
    x.strokeStyle = "#4a4f55";
    x.beginPath(); x.arc(g.cx, g.cy, g.R + x.lineWidth / 2, 0, Math.PI * 2); x.stroke();
    x.lineWidth = 1; x.strokeStyle = "rgba(255,255,255,.10)";
    x.beginPath(); x.arc(g.cx, g.cy, g.R + g.R * 0.03 + 1, 0, Math.PI * 2); x.stroke();
    return c;
  }
  // Повністю проскановний шар у «холодному» кольорі сталі.
  function strokesUpTo(x, path, sTo, g) {
    x.lineCap = "round";
    x.lineWidth = Math.max(1.2, g.gap * 0.62);
    x.strokeStyle = "#a3abb2";
    x.beginPath();
    for (var i = 0; i < path.segs.length; i++) {
      var s = path.segs[i];
      if (s.s0 >= sTo) break;
      if (s.contour) continue;
      var f = Math.min(1, (sTo - s.s0) / s.len);
      x.moveTo(s.a[0], s.a[1]);
      x.lineTo(s.a[0] + (s.b[0] - s.a[0]) * f, s.a[1] + (s.b[1] - s.a[1]) * f);
    }
    x.stroke();
    x.lineWidth = Math.max(1.4, g.gap * 0.5);
    x.strokeStyle = "#c6ccd1";
    x.beginPath();
    for (var k = 0; k < path.segs.length; k++) {
      var c = path.segs[k];
      if (c.s0 >= sTo) break;
      if (!c.contour) continue;
      var e = Math.min(1, (sTo - c.s0) / c.len);
      x.moveTo(c.a[0], c.a[1]);
      x.lineTo(c.a[0] + (c.b[0] - c.a[0]) * e, c.a[1] + (c.b[1] - c.a[1]) * e);
    }
    x.stroke();
  }
  function layerCanvas(w, h, g, idx) {
    var key = "L" + idx;
    if (cache[key]) return cache[key];
    var c = mk(w, h), x = c.getContext("2d");
    var p = path(g, idx);
    strokesUpTo(x, p, p.total, g);
    cache[key] = c;
    // тримаємо лише кілька шарів
    Object.keys(cache).forEach(function (k) {
      if (k[0] === "L" && Math.abs(parseInt(k.slice(1), 10) - idx) > 1) delete cache[k];
      if (k[0] === "P" && Math.abs(parseInt(k.slice(1), 10) - idx) > 1) delete cache[k];
    });
    return c;
  }
  function path(g, idx) {
    var key = "P" + idx;
    if (!cache[key]) cache[key] = buildPath(g, idx);
    return cache[key];
  }
  function ghostsOn(c, g) {
    var x = c.getContext("2d");
    x.fillStyle = "rgba(0,0,0,.16)";
    PARTS.forEach(function (p) { partPath(x, g, p); x.fill("evenodd"); });
    return c;
  }

  function ensure(w, h) {
    var key = w + "x" + h;
    if (cache.key === key) return;
    var g = geom(w, h);
    cache = { key: key, g: g, base: baseLayer(w, h, g), old: powderLayer(w, h, g, false),
              fresh: ghostsOn(powderLayer(w, h, g, true), g), acc: mk(w, h), accIdx: -1, accS: 0 };
    // база + старий порошок разом: на кадр на один drawImage менше
    cache.base.getContext("2d").drawImage(cache.old, 0, 0);
  }

  function lerp(a, b, k) { return a + (b - a) * k; }
  function heat(k) { // k: 0 щойно розплавлено → 1 охололо
    var stops = [[0, [255, 246, 224]], [0.15, [255, 200, 110]], [0.45, [255, 150, 60]], [0.8, [176, 112, 52]], [1, [163, 171, 178]]];
    for (var i = 1; i < stops.length; i++) {
      if (k <= stops[i][0]) {
        var f = (k - stops[i - 1][0]) / (stops[i][0] - stops[i - 1][0]);
        var a = stops[i - 1][1], b = stops[i][1];
        return "rgb(" + Math.round(lerp(a[0], b[0], f)) + "," + Math.round(lerp(a[1], b[1], f)) + "," + Math.round(lerp(a[2], b[2], f)) + ")";
      }
    }
    return "rgb(163,171,178)";
  }
  function pointAt(p, s) {
    for (var i = 0; i < p.segs.length; i++) {
      var q = p.segs[i];
      if (s <= q.s0 + q.len) {
        var f = Math.max(0, (s - q.s0) / q.len);
        return [q.a[0] + (q.b[0] - q.a[0]) * f, q.a[1] + (q.b[1] - q.a[1]) * f, i];
      }
    }
    var last = p.segs[p.segs.length - 1];
    return [last.b[0], last.b[1], p.segs.length - 1];
  }
  // Відрізок шляху [s0, s1] одним кольором (лише позначені ділянки, без стрибків).
  function strokeRange(ctx, p, s0, s1, from, mode) {
    ctx.beginPath();
    for (var i = from; i < p.segs.length; i++) {
      var q = p.segs[i];
      if (q.s0 > s1) break;
      if (q.s0 + q.len < s0) continue;
      if (mode === 1 && q.contour) continue;
      if (mode === 2 && !q.contour) continue;
      var a = Math.max(0, (s0 - q.s0) / q.len), b = Math.min(1, (s1 - q.s0) / q.len);
      ctx.moveTo(q.a[0] + (q.b[0] - q.a[0]) * a, q.a[1] + (q.b[1] - q.a[1]) * a);
      ctx.lineTo(q.a[0] + (q.b[0] - q.a[0]) * b, q.a[1] + (q.b[1] - q.a[1]) * b);
    }
    ctx.stroke();
  }
  function firstSeg(p, s) { // двійковий пошук сегмента
    var lo = 0, hi = p.segs.length - 1;
    while (lo < hi) { var m = (lo + hi) >> 1; if (p.segs[m].s0 + p.segs[m].len < s) lo = m + 1; else hi = m; }
    return lo;
  }

  function warmParts(ctx, g, p, s, alphaFn) {
    PARTS.forEach(function (part, i) {
      var a = alphaFn(i);
      if (a <= 0.01) return;
      ctx.fillStyle = "rgba(255,150,60," + a.toFixed(3) + ")";
      partPath(ctx, g, part); ctx.fill("evenodd");
    });
  }

  function recoater(ctx, g, x, w, h) {
    var bw = Math.max(8, g.R * 0.07);
    // валик свіжого порошку попереду леза
    ctx.fillStyle = "rgba(120,110,95,.55)";
    ctx.fillRect(x, 0, bw * 0.9, h);
    var gr = ctx.createLinearGradient(x - bw, 0, x, 0);
    gr.addColorStop(0, "#2a3138"); gr.addColorStop(0.45, "#7d8a95"); gr.addColorStop(1, "#39434c");
    ctx.fillStyle = gr; ctx.fillRect(x - bw, 0, bw, h);
    ctx.fillStyle = "#c9d2d8"; ctx.fillRect(x - 2, 0, 2, h);
  }

  function label(ctx, w, h, g, a) {
    if (a <= 0) return;
    var fs = Math.round(g.R * 0.22);
    ctx.save();
    ctx.globalAlpha = a;
    ctx.font = "700 " + fs + "px 'Golos Text', sans-serif";
    var tw = ctx.measureText("ГОТОВО").width;
    var bw = tw + fs * 1.1, bh = fs * 1.55;
    ctx.fillStyle = "rgba(11,8,5,.78)";
    ctx.fillRect(g.cx - bw / 2, g.cy - bh / 2, bw, bh);
    ctx.strokeStyle = "rgba(255,216,148,.55)"; ctx.lineWidth = 2;
    ctx.strokeRect(g.cx - bw / 2 + 1, g.cy - bh / 2 + 1, bw - 2, bh - 2);
    ctx.fillStyle = "#ffd894"; ctx.textAlign = "center"; ctx.textBaseline = "middle";
    ctx.fillText("ГОТОВО", g.cx, g.cy + fs * 0.04);
    ctx.restore();
  }

  (window.KMSlm = window.KMSlm || {}).melt = { draw: function (ctx, w, h, s) {
    ensure(w, h);
    var g = cache.g;
    var tt = s.t + OFFSET;
    var idx = Math.floor(tt / CYCLE);
    var ph = tt - idx * CYCLE;
    var parkX = Math.max(Math.max(8, g.R * 0.07) + 2, g.cx - g.R - 4);

    if (s.doneT !== null || s.still) {
      // Кінець: лазер вимкнено, останній шар остигає з бурштину в сталь.
      var last = s.doneT !== null ? Math.floor((s.t + OFFSET - 1e-6) / CYCLE) : idx;
      ctx.drawImage(cache.base, 0, 0);
      ctx.drawImage(cache.fresh, 0, 0);
      ctx.drawImage(layerCanvas(w, h, g, last), 0, 0);
      if (s.doneT !== null) {
        var cool = Math.max(0, 1 - s.doneT / 2.4);
        warmParts(ctx, g, null, 0, function () { return 0.55 * cool * cool; });
        recoater(ctx, g, parkX, w, h);
        if (s.finished) label(ctx, w, h, g, Math.min(1, (s.doneT - 0.5) / 0.5));
      } else {
        recoater(ctx, g, parkX, w, h);
      }
      return;
    }

    if (ph < RECOAT) {
      // Ракель іде зліва направо: позаду — свіжий порошок, попереду — минулий шар.
      var k = ph / RECOAT;
      k = k * k * (3 - 2 * k);
      var x0 = parkX, x1 = g.cx + g.R + Math.max(8, g.R * 0.07) * 1.4;
      var bx = x0 + (x1 - x0) * k;
      ctx.drawImage(cache.base, 0, 0);
      var cut = Math.max(0, Math.min(w, Math.round(bx)));
      if (cut > 0) ctx.drawImage(cache.fresh, 0, 0, cut, h, 0, 0, cut, h);
      if (cut < w) {
        ctx.drawImage(layerCanvas(w, h, g, idx - 1), cut, 0, w - cut, h, cut, 0, w - cut, h);
      }
      recoater(ctx, g, bx, w, h);
      return;
    }

    // Сканування: ванна йде штрихами по перерізах.
    var p = path(g, idx);
    var sk = (ph - RECOAT) / SCAN;
    var sHead = Math.min(1, sk / 0.97) * p.total;
    var TAIL = g.R * 1.6;              // довжина гарячого хвоста по шляху
    var sCool = Math.max(0, sHead - TAIL);
    var acc = cache.acc, ax = acc.getContext("2d");
    if (cache.accIdx !== idx || sCool < cache.accS) {
      ax.clearRect(0, 0, w, h); cache.accIdx = idx; cache.accS = 0;
    }
    if (sCool > cache.accS) {
      // домальовуємо лише те, що охололо з минулого кадру
      ax.save();
      ax.lineCap = "round";
      var from = firstSeg(p, cache.accS);
      ax.lineWidth = Math.max(1.2, g.gap * 0.62); ax.strokeStyle = "#a3abb2";
      strokeRange(ax, p, cache.accS, sCool, from, 1);
      ax.lineWidth = Math.max(1.4, g.gap * 0.5); ax.strokeStyle = "#c6ccd1";
      strokeRange(ax, p, cache.accS, sCool, from, 2);
      ax.restore();
      cache.accS = sCool;
    }
    ctx.drawImage(cache.base, 0, 0);
    ctx.drawImage(cache.fresh, 0, 0);
    // деталь, що щойно проскановано, ще тепла
    warmParts(ctx, g, p, sHead, function (i) {
      var end = p.ends[i], st = i ? p.ends[i - 1] : 0;
      if (sHead < st) return 0;
      if (sHead <= end) return 0;               // поки сканується — гріє лише хвіст
      var age = (sHead - end) / p.total * SCAN;   // секунд від кінця деталі
      return 0.30 * Math.max(0, 1 - age / 1.2);
    });
    ctx.drawImage(acc, 0, 0);
    // хвіст: 12 відрізків від холодного до білого
    ctx.save();
    ctx.lineCap = "round";
    var N = 12, from2 = firstSeg(p, sCool);
    for (var j = 0; j < N; j++) {
      var a0 = sCool + (sHead - sCool) * j / N, a1 = sCool + (sHead - sCool) * (j + 1) / N + 0.5;
      var age2 = 1 - (j + 0.5) / N;
      ctx.strokeStyle = heat(age2);
      ctx.lineWidth = Math.max(1.2, g.gap * (0.62 + 0.25 * (1 - age2)));
      strokeRange(ctx, p, a0, a1, from2);
    }
    if (sk < 0.97) {
      var hd = pointAt(p, sHead);
      var r = Math.max(2.4, g.gap * 0.75);
      ctx.fillStyle = "#ffb454";
      ctx.beginPath(); ctx.arc(hd[0], hd[1], r * 1.7, 0, Math.PI * 2); ctx.fill();
      ctx.fillStyle = "#fffaf0";
      ctx.beginPath(); ctx.arc(hd[0], hd[1], r, 0, Math.PI * 2); ctx.fill();
    }
    ctx.restore();
    recoater(ctx, g, parkX, w, h);
  } };
})();
