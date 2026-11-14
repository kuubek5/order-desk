/* Сцени екрана входу (власник 14.11.26): freza, tochky, svitlo, pult.
 *
 * Яку сцену показати, вирішує адміністратор (`app/services/login_scene.py`);
 * шаблон кладе її в `data-scene` на `.login-scene.ls-live`. Типова сцена —
 * відео — цього файлу не вантажить узагалі.
 *
 * Це лише тло. Скрипт НЕ чіпає форму: не перехоплює відправку, не читає й не
 * змінює значення полів (довжину пароля бере лише сцена «Пульт» — для шкали).
 * Єдиний виняток — перегляд з Налаштувань (`data-preview`): там відправку
 * зупиняє сам шаблон.
 *
 * Правила: один цикл кадрів, що стоїть, поки вкладка прихована; «зменшити
 * рух» у Windows = один нерухомий кадр; коли ПК не встигає (кадр довший за
 * 40 мс), сцена сама зменшує роздільність шейдера чи кількість точок.
 * Кольори — від самої сцени, не з токенів теми (як і відео).
 */
(function () {
  "use strict";
  var host = document.querySelector(".login-scene.ls-live");
  if (!host) return;
  var scene = host.getAttribute("data-scene");
  var RM = !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  var TAU = Math.PI * 2;
  var rnd = function (a, b) { return a + Math.random() * (b - a); };
  var clamp = function (v, a, b) { return Math.max(a, Math.min(b, v)); };
  var W = 0, H = 0, D = 1, T = 0, last = 0, energy = 0, busy = -1, slow = 0;
  var ptr = { x: 0.5, y: 0.5, moved: 0 };
  var cv = host.querySelector(".ls-canvas"), ctx = cv ? cv.getContext("2d") : null;
  var glc = host.querySelector(".ls-gl");
  var pass = document.getElementById("password");

  /* Де на екрані живе сцена. Міряємо СПРАВЖНЮ картку й рядок реплік над нею,
     а не вгадуємо: картка росте (панель реєстрації), репліки зсувають її вниз.
       side — ліворуч від картки, по її вертикальному центру, з полем EDGE до
              краю екрана й до картки (хрест осей ширший за диск — FAT);
       top  — над репліками, коли ліворуч тісно, а згори є місце;
       back — тьмяним тлом за карткою, коли місця немає ніде (малий телефон). */
  var EDGE = 48, FAT = 1.18;
  var cardEl = document.querySelector(".login-card"), quoteEl = document.querySelector(".dyn-text");
  var place = { mode: "side", cx: 0, cy: 0, R: 1, a: 1 };
  function measure() {
    var c = cardEl ? cardEl.getBoundingClientRect() : { left: W / 2 - 176, top: H / 2 - 150, bottom: H / 2 + 150 };
    var q = quoteEl ? quoteEl.getBoundingClientRect() : null;
    var room = c.left, free = Math.min(c.top, q ? q.top : c.top) - 12;
    if (room >= 300) {
      place = { mode: "side", cx: room / 2, cy: (c.top + c.bottom) / 2, R: Math.min(H * 0.3, (room - EDGE * 2) / 2 / FAT), a: 1 };
    } else if (free >= 150) {
      place = { mode: "top", cx: W / 2, cy: 10 + free / 2, R: Math.min(W * 0.3, (free - 20) / 2 / FAT), a: 1 };
    } else {
      place = { mode: "back", cx: W / 2, cy: H / 2, R: Math.min(W, H) * 0.42, a: 0.3 };
    }
    host.classList.toggle("ls-top", place.mode === "top");
    host.classList.toggle("ls-back", place.mode === "back");
    host.style.setProperty("--ls-room", Math.round(room) + "px");
    host.style.setProperty("--ls-cy", Math.round(place.mode === "side" ? place.cy : H / 2) + "px");
    host.style.setProperty("--ls-free", Math.round(Math.max(0, free)) + "px");
  }
  function spot() { return place; }

  function backdrop(s) {
    var g = ctx.createRadialGradient(s.cx, s.cy, s.R * 0.2, s.cx, s.cy, Math.max(W, H) * 0.8);
    g.addColorStop(0, "#1b1516"); g.addColorStop(1, "#09080a");
    ctx.globalCompositeOperation = "source-over"; ctx.fillStyle = g; ctx.fillRect(0, 0, W, H);
  }

  /* ── Фреза: диск і слід інструмента ─────────────────────────────────── */
  var freza = (function () {
    var cont = [], prog = 0, hold = 0, sparks = [];
    function build() {
      cont = []; prog = RM ? 3.6 : 0; hold = 0;
      for (var i = 0; i < 7; i++) {
        var a = i * TAU / 6 + rnd(-0.15, 0.15), d = i === 6 ? 0 : 0.58;
        var r = i === 6 ? 0.2 : rnd(0.15, 0.2), ph = [rnd(0, TAU), rnd(0, TAU), rnd(0, TAU)], rot = rnd(0, TAU), sx = rnd(0.85, 1.3);
        var pts = [];
        for (var k = 0; k <= 96; k++) {
          var t = k / 96 * TAU, rr = r * (1 + 0.16 * Math.sin(2 * t + ph[0]) + 0.09 * Math.sin(3 * t + ph[1]) + 0.05 * Math.sin(5 * t + ph[2]));
          var x = Math.cos(t) * rr * sx, y = Math.sin(t) * rr;
          pts.push([Math.cos(a) * d + x * Math.cos(rot) - y * Math.sin(rot), Math.sin(a) * d + x * Math.sin(rot) + y * Math.cos(rot)]);
        }
        cont.push(pts);
      }
    }
    function path(pts, s, upto) {
      ctx.beginPath();
      var n = Math.max(1, Math.floor(upto * (pts.length - 1)));
      for (var k = 0; k <= n; k++) {
        var x = s.cx + pts[k][0] * s.R, y = s.cy + pts[k][1] * s.R;
        if (k) ctx.lineTo(x, y); else ctx.moveTo(x, y);
      }
      return pts[n];
    }
    function frame(dt) {
      var s = spot(), R = s.R, cx = s.cx, cy = s.cy, i, k;
      backdrop(s);
      ctx.strokeStyle = "rgba(242,184,174,.05)"; ctx.lineWidth = 1; ctx.beginPath();
      for (var gx = cx % 26; gx < W; gx += 26) { ctx.moveTo(gx, 0); ctx.lineTo(gx, H); }
      for (var gy = cy % 26; gy < H; gy += 26) { ctx.moveTo(0, gy); ctx.lineTo(W, gy); }
      ctx.stroke();
      ctx.globalAlpha = s.a;
      ctx.save(); ctx.translate(cx, cy);
      ctx.strokeStyle = "rgba(242,184,174,.22)";
      var rim = R * 1.07, tick = Math.max(3, R * 0.03);
      for (k = 0; k < 72; k++) {
        var a = k / 72 * TAU + T * 0.05, l = k % 6 ? tick : tick * 2.2;
        ctx.beginPath(); ctx.moveTo(Math.cos(a) * rim, Math.sin(a) * rim); ctx.lineTo(Math.cos(a) * (rim + l), Math.sin(a) * (rim + l)); ctx.stroke();
      }
      var dg = ctx.createRadialGradient(-R * 0.3, -R * 0.35, R * 0.1, 0, 0, R);
      dg.addColorStop(0, "#3a302e"); dg.addColorStop(1, "#1a1415");
      ctx.fillStyle = dg; ctx.beginPath(); ctx.arc(0, 0, R, 0, TAU); ctx.fill();
      ctx.strokeStyle = "rgba(255,220,211,.55)"; ctx.lineWidth = 1.5; ctx.stroke();
      ctx.restore();
      var total = cont.length;
      if (prog < total) prog = Math.min(total, prog + dt * (busy >= 0 ? 7 : 0.2 + energy * 1.5));
      else if (busy < 0) { hold += dt; if (hold > 1.4) build(); }
      var idx = Math.min(total - 1, Math.floor(prog)), frac = prog >= total ? 1 : prog - idx, tool = null;
      for (i = 0; i < total; i++) {
        if (i > idx) {
          ctx.setLineDash([3, 5]); path(cont[i], s, 1); ctx.strokeStyle = "rgba(242,184,174,.16)"; ctx.lineWidth = 1; ctx.stroke(); ctx.setLineDash([]);
          continue;
        }
        var upto = i < idx ? 1 : frac, p = path(cont[i], s, upto);
        if (upto >= 1) {
          ctx.closePath(); ctx.fillStyle = "rgba(242,184,174,.10)"; ctx.fill(); ctx.strokeStyle = "rgba(242,184,174,.75)"; ctx.lineWidth = 1.4; ctx.stroke();
        } else {
          ctx.shadowColor = "#ffb066"; ctx.shadowBlur = 14; ctx.strokeStyle = "#ffe3d2"; ctx.lineWidth = 2; ctx.stroke(); ctx.shadowBlur = 0;
          tool = [cx + p[0] * R, cy + p[1] * R];
        }
      }
      if (tool) {
        ctx.strokeStyle = "rgba(255,176,102,.28)"; ctx.lineWidth = 1; ctx.beginPath();
        ctx.moveTo(tool[0], cy - R * FAT); ctx.lineTo(tool[0], cy + R * FAT); ctx.moveTo(cx - R * FAT, tool[1]); ctx.lineTo(cx + R * FAT, tool[1]); ctx.stroke();
        var n = RM ? 0 : (busy >= 0 ? 5 : 1 + Math.round(energy * 4));
        for (k = 0; k < n; k++) sparks.push({ x: tool[0], y: tool[1], vx: rnd(-90, 90), vy: rnd(-150, -20), l: rnd(0.25, 0.6), a: 1 });
        ctx.globalCompositeOperation = "lighter";
        var hg = ctx.createRadialGradient(tool[0], tool[1], 0, tool[0], tool[1], 22);
        hg.addColorStop(0, "rgba(255,240,225,1)"); hg.addColorStop(0.25, "rgba(255,176,102,.6)"); hg.addColorStop(1, "rgba(255,176,102,0)");
        ctx.fillStyle = hg; ctx.beginPath(); ctx.arc(tool[0], tool[1], 22, 0, TAU); ctx.fill();
        ctx.globalCompositeOperation = "source-over";
        if (s.mode === "side") {
          // Координати — під диском, на відстані одного рядка від хреста осей.
          ctx.font = "12px 'JetBrains Mono', monospace"; ctx.textAlign = "center"; ctx.textBaseline = "top"; ctx.fillStyle = "rgba(242,184,174,.72)";
          ctx.fillText("X " + ((tool[0] - cx) / R * 49).toFixed(2) + "    Y " + ((cy - tool[1]) / R * 49).toFixed(2), cx, cy + R * FAT + 14);
        }
      }
      ctx.globalCompositeOperation = "lighter";
      sparks = sparks.filter(function (q) { return (q.a -= dt / q.l) > 0; });
      for (i = 0; i < sparks.length; i++) {
        var q = sparks[i]; q.vy += 420 * dt; q.x += q.vx * dt; q.y += q.vy * dt;
        ctx.fillStyle = "rgba(255,190,130," + q.a.toFixed(2) + ")"; ctx.fillRect(q.x, q.y, 1.6, 1.6);
      }
      ctx.globalCompositeOperation = "source-over"; ctx.globalAlpha = 1;
    }
    build();
    return { frame: frame };
  })();

  /* ── Точки: хмара збирається в коронку, знак KM і диск ──────────────── */
  var tochky = (function () {
    var N = window.innerWidth < 700 ? 2000 : 3200, live = N, P = new Float32Array(N * 3), K = new Float32Array(N), shapes = [];
    var tgt = null, which = 0, clock = 0, ang = 0, vel = 0.35, i;
    function crown() {
      var a = new Float32Array(N * 3);
      for (var i = 0; i < N; i++) {
        var u = Math.random() * TAU, v = Math.acos(1 - Math.random() * 1.72), lobes = 1 + 0.1 * Math.cos(4 * u), sv = Math.sin(v);
        var y = Math.cos(v) * 0.95; if (v < 1) y += 0.17 * sv * Math.abs(Math.cos(2 * u)) - 0.1 * (1 - sv);
        a[i * 3] = sv * Math.cos(u) * lobes * 1.05; a[i * 3 + 1] = y + 0.1; a[i * 3 + 2] = sv * Math.sin(u) * lobes * 0.9;
      }
      return a;
    }
    function text() {
      var c = document.createElement("canvas"); c.width = 420; c.height = 200; var x = c.getContext("2d");
      x.fillStyle = "#fff"; x.textAlign = "center"; x.textBaseline = "middle"; x.font = "700 170px 'Golos Text', system-ui, sans-serif"; x.fillText("KM", 210, 108);
      var d = x.getImageData(0, 0, 420, 200).data, pool = [];
      for (var yy = 0; yy < 200; yy += 2) for (var xx = 0; xx < 420; xx += 2) if (d[(yy * 420 + xx) * 4 + 3] > 120) pool.push(xx, yy);
      var a = new Float32Array(N * 3);
      if (!pool.length) return crown();
      for (var i = 0; i < N; i++) {
        var k = (Math.random() * pool.length / 2 | 0) * 2;
        a[i * 3] = (pool[k] - 210) / 125; a[i * 3 + 1] = -(pool[k + 1] - 104) / 125; a[i * 3 + 2] = rnd(-0.09, 0.09);
      }
      return a;
    }
    function disc() {
      var a = new Float32Array(N * 3), spots = [[0, 0, 0.3], [0.62, 0.2, 0.22], [-0.55, 0.4, 0.24], [-0.3, -0.62, 0.2], [0.45, -0.55, 0.23]];
      for (var i = 0; i < N; i++) {
        var t = Math.random() * TAU, x, z;
        if (i % 5 < 2) { var r = 1.32 + rnd(-0.03, 0.03); x = Math.cos(t) * r; z = Math.sin(t) * r; }
        else { var s = spots[i % spots.length], rr = s[2] * (1 + 0.14 * Math.sin(3 * t + i % 7)); x = s[0] + Math.cos(t) * rr; z = s[1] + Math.sin(t) * rr; }
        a[i * 3] = x; a[i * 3 + 1] = rnd(-0.03, 0.03) + (i % 5 < 2 ? rnd(-0.07, 0.07) : 0); a[i * 3 + 2] = z;
      }
      return a;
    }
    function rebuild() { shapes = [crown(), text(), disc()]; tgt = shapes[which]; }
    for (i = 0; i < N; i++) { P[i * 3] = rnd(-2, 2); P[i * 3 + 1] = rnd(-2, 2); P[i * 3 + 2] = rnd(-2, 2); K[i] = rnd(1.6, 5.5); }
    rebuild();
    if (RM) for (i = 0; i < N * 3; i++) P[i] = tgt[i];
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(rebuild);
    function frame(dt) {
      var s = spot();
      backdrop(s);
      if (!RM) { clock += dt * (busy >= 0 ? 3 : 1); if (clock > 5.2) { clock = 0; which = (which + 1) % 3; tgt = shapes[which]; } }
      if (dt > 0.04) { if (++slow > 40 && live > 1200) { live = Math.max(1200, live * 0.8 | 0); slow = 0; } } else slow = 0;
      var flat = which === 1, steer = T - ptr.moved < 2 ? (ptr.x - 0.5) * 1.6 : 0;
      vel += (((flat ? 0 : 0.35) + energy * 0.8 + steer) - vel) * Math.min(1, dt * 2);
      if (flat) ang += (Math.sin(T * 0.7) * 0.28 + steer * 0.3 - ang) * Math.min(1, dt * 2.4); else ang += vel * dt;
      var ca = Math.cos(ang), sa = Math.sin(ang), tilt = which === 2 ? 0.95 : 0.18, ct = Math.cos(tilt), st = Math.sin(tilt);
      var sc = s.R / 1.45, jit = energy * 0.05;
      ctx.globalCompositeOperation = "lighter";
      for (var i = 0; i < live; i++) {
        var j = i * 3, e = 1 - Math.exp(-dt * K[i]);
        P[j] += (tgt[j] - P[j]) * e + rnd(-jit, jit); P[j + 1] += (tgt[j + 1] - P[j + 1]) * e + rnd(-jit, jit); P[j + 2] += (tgt[j + 2] - P[j + 2]) * e + rnd(-jit, jit);
        var x = P[j] * ca + P[j + 2] * sa, z0 = -P[j] * sa + P[j + 2] * ca, y = P[j + 1] * ct - z0 * st, z = P[j + 1] * st + z0 * ct;
        var f = 3 / (3 + z), al = clamp(0.2 + (1 - z) * 0.34, 0.08, 0.95) * s.a, size = f * 1.5 + (i % 9 === 0 ? 1.1 : 0);
        ctx.fillStyle = "rgba(242,184,174," + al.toFixed(2) + ")";
        ctx.fillRect(s.cx + x * f * sc, s.cy - y * f * sc, size, size);
      }
      ctx.globalCompositeOperation = "source-over";
    }
    return { frame: frame };
  })();

  /* ── Світло: фон рахує відеокарта ───────────────────────────────────── */
  var svitlo = (function () {
    var gl = null, U = {}, ok = false, q = 0.5;
    var FS = "precision highp float;uniform vec2 r;uniform float t;uniform vec2 p;uniform float e;uniform float s;" +
      "float h(vec2 q){return fract(sin(dot(q,vec2(127.1,311.7)))*43758.5453);}" +
      "float n(vec2 q){vec2 i=floor(q),f=fract(q);f=f*f*(3.-2.*f);return mix(mix(h(i),h(i+vec2(1.,0.)),f.x),mix(h(i+vec2(0.,1.)),h(i+vec2(1.,1.)),f.x),f.y);}" +
      "float fbm(vec2 q){float a=.5,v=0.;for(int i=0;i<5;i++){v+=a*n(q);q=q*2.03+vec2(1.7,9.2);a*=.5;}return v;}" +
      "void main(){vec2 uv=gl_FragCoord.xy/r;vec2 q=(gl_FragCoord.xy-.5*r)/r.y;float tt=t*.07;" +
      "vec2 w=vec2(fbm(q*1.5+vec2(tt,-tt)),fbm(q*1.5+vec2(-tt*1.3,tt)+4.));" +
      "float f=fbm(q*2.1+w*1.9+vec2(0.,tt*2.));" +
      "vec3 c=vec3(.03,.028,.04);" +
      "c=mix(c,vec3(.50,.38,.95)*.6,smoothstep(.3,.78,fbm(q*1.15-w+tt))*.75);" +
      "c=mix(c,vec3(.96,.70,.66),smoothstep(.42,.82,f)*.62);" +
      "c+=vec3(1.,.62,.33)*pow(max(f-.52,0.),2.)*3.2*(1.+e*1.6);" +
      "vec2 pp=(p-.5)*vec2(r.x/r.y,1.);float d=length(q-pp);" +
      "c+=vec3(1.,.84,.78)*.2*exp(-d*d*7.);" +
      "if(s>=0.){float rr=s*1.5;float ring=exp(-pow((length(q)-rr)*8.,2.));c+=vec3(1.,.9,.84)*ring*(1.-smoothstep(.2,1.5,s))*1.3;}" +
      "c*=1.-.9*dot(uv-.5,uv-.5);c+=(h(gl_FragCoord.xy+t)-.5)*.03;" +
      "gl_FragColor=vec4(c,1.);}";
    function init() {
      try {
        gl = glc.getContext("webgl", { antialias: false, alpha: false });
        if (!gl) throw new Error("no webgl");
        var mk = function (type, src) {
          var sh = gl.createShader(type); gl.shaderSource(sh, src); gl.compileShader(sh);
          if (!gl.getShaderParameter(sh, gl.COMPILE_STATUS)) throw new Error("shader");
          return sh;
        };
        var pr = gl.createProgram();
        gl.attachShader(pr, mk(gl.VERTEX_SHADER, "attribute vec2 a;void main(){gl_Position=vec4(a,0.,1.);}"));
        gl.attachShader(pr, mk(gl.FRAGMENT_SHADER, FS)); gl.linkProgram(pr);
        if (!gl.getProgramParameter(pr, gl.LINK_STATUS)) throw new Error("link");
        gl.useProgram(pr);
        gl.bindBuffer(gl.ARRAY_BUFFER, gl.createBuffer()); gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);
        var loc = gl.getAttribLocation(pr, "a"); gl.enableVertexAttribArray(loc); gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 0, 0);
        ["r", "t", "p", "e", "s"].forEach(function (k) { U[k] = gl.getUniformLocation(pr, k); });
        ok = true;
      } catch (err) {
        // Без WebGL сцена не падає: лишається нерухомий градієнт із CSS.
        ok = false; gl = null; host.classList.add("ls-nogl");
        if (window.console) console.info("login scene: WebGL недоступний, показую градієнт", err && err.message);
      }
    }
    function size() { glc.width = Math.max(2, W * q | 0); glc.height = Math.max(2, H * q | 0); }
    function frame(dt) {
      if (!gl && !host.classList.contains("ls-nogl")) { init(); size(); }
      if (!ok) return;
      if (dt > 0.04) { if (++slow > 40 && q > 0.25) { q *= 0.75; slow = 0; size(); } } else slow = 0;
      gl.viewport(0, 0, glc.width, glc.height);
      gl.uniform2f(U.r, glc.width, glc.height); gl.uniform1f(U.t, T); gl.uniform2f(U.p, ptr.x, 1 - ptr.y); gl.uniform1f(U.e, energy); gl.uniform1f(U.s, busy);
      gl.drawArrays(gl.TRIANGLES, 0, 3);
    }
    return { frame: frame, size: size };
  })();

  /* ── Пульт: креслення лініями, набірна назва, шпиндель ──────────────── */
  var pult = (function () {
    var name = host.querySelector(".lsp-name"), rpmEl = host.querySelector(".lsp-rpm"), keyEl = host.querySelector(".lsp-key");
    var bar = host.querySelector(".lsp-bar"), state = host.querySelector(".lsp-state");
    if (!name || !bar) return { frame: function () {} };
    var lines = Array.prototype.slice.call(host.querySelectorAll(".lsp-ln")), ring = host.querySelector(".lsp-ring");
    var WORD = "KuubMill", GLYPHS = "ABCDEFGHKLMNPRSTUVXZ0123456789#/", rpm = 0, said = "";
    for (var i = 0; i < 14; i++) bar.appendChild(document.createElement("i"));
    (function scramble() {
      var spans = WORD.split("").map(function () { return name.appendChild(document.createElement("span")); }), step = 0;
      name.firstChild && name.firstChild.nodeType === 3 && name.removeChild(name.firstChild);
      if (RM) { spans.forEach(function (el, k) { el.textContent = WORD[k]; }); return; }
      (function tick() {
        spans.forEach(function (el, k) {
          var done = step > 6 + k * 3;
          el.textContent = done ? WORD[k] : GLYPHS[Math.random() * GLYPHS.length | 0]; el.className = done ? "" : "x";
        });
        if (step++ < 8 + WORD.length * 3) setTimeout(tick, 42);
      })();
    })();
    function frame(dt) {
      var typed = pass ? pass.value.length : 0, on = Math.min(14, Math.round(typed * 1.75));
      Array.prototype.forEach.call(bar.children, function (el, k) { el.className = k < on ? "on" : ""; });
      keyEl.textContent = typed ? new Array(Math.min(typed, 8) + 1).join("•") : "—";
      var want = busy >= 0 ? 42000 : Math.min(1, energy) * 18000 + typed * 1400;
      rpm += (want - rpm) * Math.min(1, dt * (busy >= 0 ? 3 : 2));
      var shown = Math.round(rpm / 10) * 10;
      rpmEl.textContent = shown ? String(shown).replace(/\B(?=(\d{3})+(?!\d))/g, " ") : "0";
      var text = busy < 0 ? "очікує оператора" : busy < 0.6 ? "перевірка ключа" : "шпиндель розкручено";
      if (text !== said) { said = text; state.textContent = text; }
      lines.forEach(function (el, k) {
        var u = RM ? 1 : clamp((T - k * 0.14) / 1.4, 0, 1), e = 1 - Math.pow(1 - u, 3);
        if (el === ring && rpm > 600) { el.style.strokeDasharray = ".03 .02"; el.style.strokeDashoffset = (-T * rpm / 42000 * 0.6).toFixed(4); }
        else { el.style.strokeDasharray = "1"; el.style.strokeDashoffset = (1 - e).toFixed(4); }
      });
    }
    return { frame: frame };
  })();

  var active = { freza: freza, tochky: tochky, svitlo: svitlo, pult: pult }[scene];
  if (!active || ((scene === "freza" || scene === "tochky") && !ctx) || (scene === "svitlo" && !glc)) return;

  function resize() {
    W = window.innerWidth; H = window.innerHeight; D = Math.min(window.devicePixelRatio || 1, 2);
    if (cv) { cv.width = W * D; cv.height = H * D; ctx.setTransform(D, 0, 0, D, 0, 0); }
    if (scene === "svitlo") svitlo.size();
    measure();
  }
  function loop(ts) {
    var dt = Math.min(0.05, (ts - last) / 1000 || 0); last = ts;
    if (!document.hidden) {
      T += dt; energy *= Math.exp(-dt * 2.4); if (busy >= 0) busy += dt;
      if (W) active.frame(dt);
    }
    window.requestAnimationFrame(loop);
  }
  document.addEventListener("input", function (e) {
    if (!e.target || !e.target.closest || !e.target.closest(".login-card")) return;
    energy = Math.min(1.6, energy + 0.5);
    if (RM) active.frame(0.016);
  });
  // Відправку НЕ зупиняємо: сцена лише «розганяється», поки сервер відповідає.
  document.addEventListener("submit", function (e) {
    if (e.target && e.target.closest && e.target.closest(".login-card")) {
      busy = 0;
      if (host.hasAttribute("data-preview")) setTimeout(function () { busy = -1; }, 2600);
    }
  });
  window.addEventListener("pointermove", function (e) { ptr.x = e.clientX / Math.max(1, W); ptr.y = e.clientY / Math.max(1, H); ptr.moved = T; });
  window.addEventListener("resize", function () { resize(); active.frame(0); });
  resize();
  // Картка росте (панель реєстрації, рядок помилки), шрифти довантажуються —
  // місце сцени перераховується, інакше диск опинявся б під карткою.
  if (window.ResizeObserver && cardEl) new ResizeObserver(function () { measure(); if (RM) active.frame(0); }).observe(cardEl);
  window.addEventListener("load", function () { measure(); if (RM) active.frame(0); });
  if (RM) { T = 3; active.frame(0.016); } else window.requestAnimationFrame(loop);
})();
