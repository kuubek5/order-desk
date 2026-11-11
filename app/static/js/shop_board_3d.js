/* 3D-робота на плитці верстата (табло цеху, власник 11.11.26 — варіант А
   «велика й малі»; мокап design/tv-board-concept.js).

   Плитка, що фрезерує, несе `data-models` (адреси моделей за секретом табло,
   `board_models` на сервері) і `data-pct` (відсоток верстата). Скрипт:
     • вантажить моделі один раз (кеш за адресою),
     • малює поточну одиницю крупно: знизу тверда, згори сітка, межа йде з
       відсотком; на зрізі залита товща стінки (трафарет, лише на замкненій
       моделі); решта одиниць малими рядами під нею (готові тверді, наступні
       сіткою, поточна в кільці) і «готово N з M».
   «Поточна одиниця» — оцінка рівними частками відсотка за одиницями моделей:
   верстат сам не каже, яку коронку ріже.

   Одна WebGL-сцена на все табло: кадр плитки малюється у спільне полотно й
   копіюється в її 2D-полотно (браузер обмежує число WebGL-контекстів).
   До 30 кадрів/с — замір на ПК телевізора 11.11.26.

   Розміри підбираються біля телевізора без релізу — параметри адреси, які
   сторінка запам'ятовує: ?big=1.2 (велика), ?small=0.8 (малі), ?zone=1.3
   (висота зони вгору), ?reset3d=1 — повернути типові. Живий шматок табло
   замінюється раз на 30 с; `KM3D.apply` повертає полотна на нові плитки. */
(function(){
"use strict";
if (typeof THREE === "undefined") return;

var BUF = 1024, FRAME_MS = 33;
var Q = new URLSearchParams(location.search);
var SIZE = {big:1, small:1, zone:1}, STORE = window.KMStore, SIZE_KEY = "board-3d-size";
try {
  if (STORE && Q.get("reset3d") === "1") STORE.remove(SIZE_KEY);
  var saved = JSON.parse((STORE && STORE.get(SIZE_KEY)) || "{}");
  Object.keys(SIZE).forEach(function(k){ if (typeof saved[k] === "number") SIZE[k] = saved[k]; });
} catch (_e) { /* зіпсований запис — типові розміри */ }
var changed = false;
Object.keys(SIZE).forEach(function(k){
  var v = parseFloat(Q.get(k));
  if (isFinite(v) && v > 0.2 && v < 4){ SIZE[k] = v; changed = true; }
});
if (changed && STORE) STORE.set(SIZE_KEY, JSON.stringify(SIZE));

function smooth(a, b, x){ var t = Math.min(1, Math.max(0, (x - a) / (b - a))); return t * t * (3 - 2 * t); }
function mix(a, b){ var o = {}, k; for (k in a) o[k] = a[k]; for (k in b) o[k] = b[k]; return o; }

// ── Моделі ──
function bytes(b64){ var bin = atob(b64), u = new Uint8Array(bin.length); for (var i = 0; i < bin.length; i++) u[i] = bin.charCodeAt(i); return u.buffer; }
function points(b64, S){ var q = new Int16Array(bytes(b64)), f = new Float32Array(q.length); for (var i = 0; i < q.length; i++) f[i] = q[i] / 32767 * S; return f; }
function lineGeo(b64, S){ var g = new THREE.BufferGeometry(); g.setAttribute("position", new THREE.BufferAttribute(points(b64, S), 3)); return g; }
function index(b64, type){ return new THREE.BufferAttribute(type === "<u4" ? new Uint32Array(bytes(b64)) : new Uint16Array(bytes(b64)), 1); }
function buildModel(m){
  var g = new THREE.BufferGeometry();
  g.setAttribute("position", new THREE.BufferAttribute(points(m.pos, m.S), 3));
  g.setIndex(index(m.idx, m.it)); g.computeVertexNormals();
  var lo = new THREE.BufferGeometry();
  lo.setAttribute("position", new THREE.BufferAttribute(points(m.lo, m.S), 3));
  lo.setIndex(index(m.loi, m.lit)); lo.computeVertexNormals();
  return {geo:g, mesh:lineGeo(m.wire, m.S), lo:lo, small:lineGeo(m.small, m.S), box:m.box,
          closed:!!m.closed, units:m.units || 1};
}
var MODELS = {};                         // адреса → Promise<модель>
function model(url){
  if (!MODELS[url]){
    MODELS[url] = fetch(url).then(function(r){ if (!r.ok) throw new Error(r.status); return r.json(); })
      .then(buildModel)
      .catch(function(){ delete MODELS[url]; return null; });
  }
  return MODELS[url];
}

var renderer = null, ENV = null;
try { renderer = new THREE.WebGLRenderer({antialias:true, alpha:true, preserveDrawingBuffer:true, stencil:true}); } catch (_e) { renderer = null; }
if (!renderer) return;
renderer.setPixelRatio(1); renderer.setSize(BUF, BUF, false);
renderer.localClippingEnabled = true; renderer.outputEncoding = THREE.sRGBEncoding;
renderer.toneMapping = THREE.ACESFilmicToneMapping; renderer.toneMappingExposure = 0.86;
renderer.setClearColor(0x000000, 0);
try {
  var es = new THREE.Scene();
  es.add(new THREE.Mesh(new THREE.SphereGeometry(20, 24, 16), new THREE.MeshBasicMaterial({color:0x130e08, side:THREE.BackSide})));
  var panel = function(w, h, x, y, z, r, g, b){
    var m = new THREE.Mesh(new THREE.PlaneGeometry(w, h), new THREE.MeshBasicMaterial({side:THREE.DoubleSide}));
    m.material.color.setRGB(r, g, b); m.position.set(x, y, z); m.lookAt(0, 0, 0); es.add(m);
  };
  panel(9, 9, 2, 9, 3, 2.6, 2.5, 2.3); panel(5, 8, -9, 2, -3, 1.9, 1.2, 0.5); panel(4, 6, 8, 1, 5, 0.9, 0.85, 0.8);
  var pm = new THREE.PMREMGenerator(renderer); ENV = pm.fromScene(es, 0.035).texture; pm.dispose();
} catch (_e2) { ENV = null; }

var TONES = {
  zr:{color:0xd9caac, roughness:0.36, clearcoat:0.35, clearcoatRoughness:0.4},
  pm:{color:0xd6bb86, roughness:0.2, clearcoat:0.9, clearcoatRoughness:0.18},
  ml:{color:0xe0d5bf, roughness:0.32, clearcoat:0.45, clearcoatRoughness:0.35},
  ti:{color:0xb9bcc2, roughness:0.3, metalness:0.75, clearcoat:0.2, clearcoatRoughness:0.4},
  wx:{color:0x5d7fa8, roughness:0.45, clearcoat:0.25, clearcoatRoughness:0.5}
};
var CUT = {zr:0xb5a584, pm:0xb2965f, ml:0xbcae93, ti:0x8d9097, wx:0x46607f};
function toneOf(text){
  text = text || "";
  return /пмма|pmma/i.test(text) ? "pm" : /титан|titan|ti\b/i.test(text) ? "ti"
    : /віск|wax/i.test(text) ? "wx" : /мульти|multi|emo|emotion/i.test(text) ? "ml" : "zr";
}
function stencilMat(side, op, plane){
  return new THREE.MeshBasicMaterial({side:side, clippingPlanes:[plane], depthWrite:false, depthTest:false, colorWrite:false,
    stencilWrite:true, stencilFunc:THREE.AlwaysStencilFunc, stencilFail:op, stencilZFail:op, stencilZPass:op});
}
function norm(M){
  var b0 = M.box[0], b1 = M.box[1];
  var R = Math.sqrt(Math.pow(Math.max(-b0[0], b1[0]), 2) + Math.pow(Math.max(-b0[2], b1[2]), 2)) * 0.86;
  var H = b1[1] - b0[1];
  return {R:R, H:H, k:1 / Math.max(R, H * 0.62)};
}
function makeHero(M, tone){
  var below = new THREE.Plane(new THREE.Vector3(0, -1, 0), 100), above = new THREE.Plane(new THREE.Vector3(0, 1, 0), 100);
  var solidMat = new THREE.MeshPhysicalMaterial(mix({side:THREE.DoubleSide, envMap:ENV, envMapIntensity:0.5, clippingPlanes:[below]}, TONES[tone]));
  var wireMat = new THREE.LineBasicMaterial({color:0xffb454, transparent:true, opacity:0.5, clippingPlanes:[above], toneMapped:false});
  var n = norm(M), inner = new THREE.Group(), group = new THREE.Group(); group.add(inner);
  inner.add(new THREE.Mesh(M.geo, solidMat), new THREE.LineSegments(M.mesh, wireMat));
  if (M.closed){
    // Трафарет товщі стінки — лише на замкненій моделі; на відкритій він
    // залив би весь зріз суцільним диском.
    var back = new THREE.Mesh(M.geo, stencilMat(THREE.BackSide, THREE.IncrementWrapStencilOp, below));
    var front = new THREE.Mesh(M.geo, stencilMat(THREE.FrontSide, THREE.DecrementWrapStencilOp, below));
    back.renderOrder = front.renderOrder = 1; inner.add(back, front);
  }
  inner.scale.setScalar(n.k);
  return {group:group, below:below, above:above, R:n.R * n.k, h:n.H * n.k, closed:M.closed};
}
function basic(color, opacity, extra){
  return new THREE.MeshBasicMaterial(mix({color:color, transparent:true, opacity:opacity, side:THREE.DoubleSide, depthWrite:false, toneMapped:false}, extra || {}));
}

var VIEW_H = 2.68;
function makeView(list, tone){
  var scene = new THREE.Scene();
  scene.add(new THREE.HemisphereLight(0xfff1dc, 0x2a1c0e, 0.2));
  var key = new THREE.DirectionalLight(0xfff4e2, 0.48); key.position.set(2.5, 4, 3.5); scene.add(key);
  var rim = new THREE.DirectionalLight(0xffb454, 0.45); rim.position.set(-3, 1.6, -2.6); scene.add(rim);
  var cam = new THREE.PerspectiveCamera(24, 2, 0.3, 60);
  cam.position.set(0, 1.6, 6.1); cam.lookAt(0, 0, 0);
  var n = list.length, rows = n > 1 ? (n > 8 ? 2 : 1) : 0, cols = rows ? Math.ceil(n / rows) : 0;
  var solidMat = new THREE.MeshPhysicalMaterial(mix({side:THREE.DoubleSide, envMap:ENV, envMapIntensity:0.5}, TONES[tone]));
  var ghostMat = new THREE.LineBasicMaterial({color:0xffb454, transparent:true, opacity:0.4, toneMapped:false});
  var stage = new THREE.Group(); scene.add(stage);
  var slots = [], heroes = [], total = 0, i;
  for (i = 0; i < n; i++){
    var M = list[i], q = norm(M);
    total += M.units;
    if (rows){
      var g = new THREE.Group(); g.rotation.y = i * 1.9; stage.add(g);
      var solid = new THREE.Mesh(M.lo, solidMat), wire = new THREE.LineSegments(M.small, ghostMat);
      solid.scale.setScalar(q.k); wire.scale.setScalar(q.k);
      g.add(solid, wire);
      slots.push({g:g, solid:solid, wire:wire});
    }
    var hero = makeHero(M, tone); hero.group.visible = false; scene.add(hero.group); heroes.push(hero);
  }
  var cap = new THREE.Mesh(new THREE.PlaneGeometry(8, 8), new THREE.MeshStandardMaterial({color:CUT[tone], roughness:0.75,
    stencilWrite:true, stencilRef:0, stencilFunc:THREE.NotEqualStencilFunc, stencilFail:THREE.ReplaceStencilOp,
    stencilZFail:THREE.ReplaceStencilOp, stencilZPass:THREE.ReplaceStencilOp}));
  cap.rotation.x = -Math.PI / 2; cap.renderOrder = 1.1;
  cap.onAfterRender = function(r){ r.clearStencil(); };
  var scan = new THREE.Mesh(new THREE.CircleGeometry(1, 64), basic(0xffb454, 0.1, {blending:THREE.AdditiveBlending}));
  var edge = new THREE.Mesh(new THREE.RingGeometry(0.965, 1, 96), basic(0xffc56b, 0.85));
  scan.rotation.x = edge.rotation.x = -Math.PI / 2; scene.add(cap, scan, edge);
  var mark = null;
  if (rows){ mark = new THREE.Mesh(new THREE.RingGeometry(1.22, 1.42, 40), basic(0xffc56b, 0.95)); stage.add(mark); }
  var L = {heroY:0, heroH:2.0, sK:0, rowY:[]};
  // Межі одиниць у частках роботи: міст на 3 зуби займає 3 частки, не одну.
  var bounds = [0], acc = 0;
  for (i = 0; i < n; i++){ acc += list[i].units; bounds.push(acc / total); }
  return {
    scene:scene, cam:cam, total:total,
    fit:function(aspect){
      var visW = VIEW_H * aspect;
      if (!rows){ L.heroY = 0.02; L.heroH = 1.95; return; }
      L.sK = Math.min(0.26 * SIZE.small, visW * 0.9 / (cols * 2.3));
      stage.scale.setScalar(L.sK);
      var smallH = 1.7 * L.sK, gap = Math.max(0.36, smallH * 1.12);
      var bottom = -VIEW_H / 2 + 0.12 + smallH / 2;
      L.rowY = rows === 2 ? [bottom + gap, bottom] : [bottom];
      var rowsTop = L.rowY[0] + smallH / 2 + 0.16;
      L.heroH = VIEW_H / 2 - 0.1 - rowsTop;
      L.heroY = rowsTop + L.heroH / 2;
      for (var j = 0; j < n; j++){
        var r = Math.floor(j / cols), c = j % cols, inRow = Math.min(cols, n - r * cols);
        slots[j].g.position.set((c - (inRow - 1) / 2) * 2.3, L.rowY[r] / L.sK, 0);
      }
    },
    update:function(pct, t){
      var f = Math.min(0.99999, Math.max(0, pct / 100)), cur = 0, j;
      while (cur < n - 1 && f >= bounds[cur + 1]) cur++;
      var u = (f - bounds[cur]) / Math.max(1e-6, bounds[cur + 1] - bounds[cur]);
      for (j = 0; j < slots.length; j++){ slots[j].solid.visible = j < cur; slots[j].wire.visible = j >= cur; }
      for (j = 0; j < heroes.length; j++) heroes[j].group.visible = false;
      var o = heroes[cur];
      var ws = Math.min(L.heroH / o.h, VIEW_H * cam.aspect * 0.42 / o.R / 2, 1.9) * SIZE.big;
      o.group.visible = true; o.group.scale.setScalar(ws); o.group.position.set(0, L.heroY, 0);
      o.group.rotation.y = t * 2 * Math.PI / 12 + cur * 1.9;
      if (mark){ mark.position.copy(slots[cur].g.position); mark.quaternion.copy(cam.quaternion); }
      var a0 = L.heroY - o.h / 2 * ws, a1 = L.heroY + o.h / 2 * ws;
      var level = a0 - 0.01 + smooth(0, 1, u) * (a1 - a0 + 0.02);
      o.below.constant = level; o.above.constant = -level;
      var er = o.R * ws * 1.18;
      cap.visible = o.closed;
      scan.scale.setScalar(er); edge.scale.setScalar(er);
      cap.position.set(0, level, 0); scan.position.copy(cap.position); edge.position.copy(cap.position);
      return Math.round(f * total * 1000) / 1000;
    }
  };
}

// ── Плитки ──
var VIEWS = {};                          // ключ верстата → вигляд
function place(it){
  var el = it.el, box = el.getBoundingClientRect(), work = el.querySelector(".mc-work"), id = el.querySelector(".mc-id");
  var label = id && (id.querySelector("em") || id);
  var bottom = (label ? label.getBoundingClientRect().top : box.top + box.height * 0.62) - box.top - 4;
  var top = (work ? work.getBoundingClientRect().bottom : box.top + box.height * 0.3) - box.top + 2;
  var h = Math.max(40, bottom - top) * SIZE.zone;
  var minTop = el.querySelector(".mc-top") ? el.querySelector(".mc-top").getBoundingClientRect().bottom - box.top : 0;
  top = Math.max(minTop, bottom - h);
  it.canvas.style.top = top + "px"; it.canvas.style.height = Math.max(40, bottom - top) + "px";
}
function attach(it, el){
  it.el = el;
  if (it.canvas.parentNode !== el) el.insertBefore(it.canvas, el.querySelector(".mc-top"));
  el.classList.add("has-3d");
  var bot = el.querySelector(".mc-bot");
  if (bot && it.unit.parentNode !== bot) bot.appendChild(it.unit);
  it.target = Number(el.getAttribute("data-pct")) || 0;
  if (it.shown == null || it.target < it.shown - 1) it.shown = it.target;  // нова програма — одразу
  it.w = 0;                                                              // переміряти місце
}
function apply(root){
  var seen = {};
  (root || document).querySelectorAll(".mc[data-models]").forEach(function(el){
    var keyName = el.getAttribute("data-key") || "", raw = el.getAttribute("data-models") || "[]", list;
    try { list = JSON.parse(raw); } catch (_e) { return; }
    if (!list.length) return;
    var sig = list.map(function(m){ return m.url; }).join("|"), tone = toneOf(el.getAttribute("data-mat"));
    seen[keyName] = true;
    var it = VIEWS[keyName];
    if (it && it.sig === sig + tone){ attach(it, el); return; }
    if (it && it.canvas.parentNode) it.canvas.parentNode.removeChild(it.canvas);
    var canvas = document.createElement("canvas"); canvas.className = "g3";
    var unit = document.createElement("span"); unit.className = "unit3";
    it = VIEWS[keyName] = {sig:sig + tone, canvas:canvas, ctx:canvas.getContext("2d"), unit:unit, view:null, w:0, h:0, done:-1};
    attach(it, el);
    var mine = it;
    Promise.all(list.map(function(m){ return model(m.url); })).then(function(models){
      if (VIEWS[keyName] !== mine) return;
      models = models.filter(Boolean);
      if (!models.length){ el.classList.remove("has-3d"); return; }
      mine.view = makeView(models, tone);
    });
  });
  Object.keys(VIEWS).forEach(function(k){
    if (seen[k]) return;
    var it = VIEWS[k];
    if (it.canvas.parentNode) it.canvas.parentNode.removeChild(it.canvas);
    if (it.unit.parentNode) it.unit.parentNode.removeChild(it.unit);
    delete VIEWS[k];
  });
}

function frame(now){
  var t = now / 1000;
  Object.keys(VIEWS).forEach(function(k){
    var it = VIEWS[k];
    if (!it.view || !it.el.isConnected) return;
    if (!it.w) place(it);
    var w = Math.min(BUF, Math.round(it.canvas.clientWidth)), h = Math.min(BUF, Math.round(it.canvas.clientHeight));
    if (!w || !h) return;
    if (w !== it.w || h !== it.h){
      it.canvas.width = it.w = w; it.canvas.height = it.h = h;
      it.view.cam.aspect = w / h; it.view.cam.updateProjectionMatrix(); it.view.fit(w / h);
    }
    // Відсоток приходить раз на 30 с — межа зрізу доганяє його плавно.
    it.shown += (it.target - it.shown) * 0.02;
    var done = it.view.update(it.shown, t);
    renderer.setViewport(0, 0, w, h); renderer.setScissor(0, 0, w, h); renderer.setScissorTest(true);
    renderer.clear(); renderer.render(it.view.scene, it.view.cam);
    it.ctx.clearRect(0, 0, w, h);
    it.ctx.drawImage(renderer.domElement, 0, BUF - h, w, h, 0, 0, w, h);
    var whole = Math.floor(done);
    if (whole !== it.done){
      it.done = whole;
      it.unit.innerHTML = it.view.total > 1 ? "готово <b>" + whole + "</b> з " + it.view.total : "";
    }
  });
}

window.KM3D = {apply:apply, size:SIZE};
var last = 0;
function tick(now){
  if (now - last >= FRAME_MS){ last = now; try { frame(now); } catch (_e) { /* кадр пропускаємо, табло живе */ } }
  requestAnimationFrame(tick);
}
window.addEventListener("resize", function(){ Object.keys(VIEWS).forEach(function(k){ VIEWS[k].w = 0; }); });
apply(document);
requestAnimationFrame(tick);
})();
