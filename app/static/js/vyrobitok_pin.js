// ПІН-гейт розділу «Виробіток»: split-flap табло «ВВЕДІТЬ ПІН-КОД» + numeric
// keypad. Табло механічно перегортає літери (верхня половина складається
// донизу, нижня підіймається — справжній Vestaboard), потім проявляється
// клавіатура. Ввід збирається в приховане поле name="pin"; форму шле лише
// кнопка «Відкрити» або Enter — цифрові клавіші type="button" не сабмітять.
// Розмітку плиток і крапок домальовує цей файл, стилі — vyrobitok.css.
(function () {
  var form = document.querySelector('form.vt-pin[data-vtpin]');
  if (!form) return;

  var LINES = ['ВВЕДІТЬ', 'ПІН-КОД'];
  var CHARS = ' 0123456789АБВГДЕЄЖЗИІЇКЛМНОПРСТУФХЦЧШЩЬЮЯ-';
  var MAX = 12;

  var board = form.querySelector('.vt-board');
  var after = form.querySelector('.vt-pin-after');
  var dotsEl = form.querySelector('.vt-dots');
  var hidden = form.querySelector('input[name="pin"]');
  var errEl = form.querySelector('.vt-pin-err');
  var card = form.querySelector('.vt-pin-card');
  var buf = '';

  function rnd() { return CHARS[Math.floor(Math.random() * CHARS.length)]; }

  // Один механічний фліп плитки з поточного символу на новий: верхня половина
  // (старий символ) падає донизу, нижня (новий) підіймається з затримкою в
  // півтривалості — звідси характерний «розлам» split-flap.
  function flipTo(el, nc, dur) {
    var stT = el.querySelector('.st.top .ch'), stB = el.querySelector('.st.bot .ch');
    var flT = el.querySelector('.fl.top .ch'), flB = el.querySelector('.fl.bot .ch');
    var flTd = el.querySelector('.fl.top'), flBd = el.querySelector('.fl.bot');
    var cur = el.dataset.ch;
    flT.textContent = cur; stT.textContent = nc; flB.textContent = nc; stB.textContent = cur;
    flTd.style.transition = 'none'; flBd.style.transition = 'none';
    flTd.style.transform = 'rotateX(0deg)'; flBd.style.transform = 'rotateX(90deg)';
    void el.offsetWidth;
    flTd.style.transition = 'transform ' + (dur / 2) + 'ms ease-in';
    flTd.style.transform = 'rotateX(-90deg)';
    setTimeout(function () {
      flBd.style.transition = 'transform ' + (dur / 2) + 'ms ease-out';
      flBd.style.transform = 'rotateX(0deg)';
      setTimeout(function () { stB.textContent = nc; el.dataset.ch = nc; }, dur / 2);
    }, dur / 2);
  }

  function makeCell(target) {
    var el = document.createElement('div');
    el.className = 'vt-fc' + (target === ' ' ? ' blank' : '');
    el.dataset.ch = ' ';
    el.innerHTML =
      '<div class="half st top"><span class="ch"> </span></div>' +
      '<div class="half st bot"><span class="ch"> </span></div>' +
      '<div class="half fl top"><span class="ch"> </span></div>' +
      '<div class="half fl bot"><span class="ch"> </span></div>';
    return { el: el, target: target };
  }

  // Кожна плитка прокручує 18–29 випадкових символів і осідає на цільовому;
  // стартова затримка росте зліва направо — стовпці осідають хвилею.
  function buildAndRun() {
    board.innerHTML = '';
    var cells = [];
    LINES.forEach(function (line) {
      var row = document.createElement('div'); row.className = 'vt-brow';
      line.split('').forEach(function (ch) { var c = makeCell(ch); row.appendChild(c.el); cells.push(c); });
      board.appendChild(row);
    });
    var col = 0, remaining = cells.filter(function (c) { return c.target !== ' '; }).length;
    cells.forEach(function (c) {
      if (c.target === ' ') return;
      var steps = 18 + Math.floor(Math.random() * 12), i = 0, delay = col * 45; col++;
      setTimeout(function tick() {
        if (i < steps) { flipTo(c.el, rnd(), 95); i++; setTimeout(tick, 60); }
        else {
          flipTo(c.el, c.target, 300); c.el.classList.add('set');
          if (--remaining === 0) setTimeout(function () { after.classList.add('show'); }, 350);
        }
      }, delay);
    });
  }

  function renderDots() {
    dotsEl.innerHTML = '';
    for (var i = 0; i < buf.length; i++) dotsEl.appendChild(document.createElement('i'));
    hidden.value = buf;
  }

  function shake() {
    card.classList.add('shake');
    setTimeout(function () { card.classList.remove('shake'); }, 420);
  }

  form.querySelectorAll('.vt-kp button').forEach(function (b) {
    b.addEventListener('click', function () {
      errEl.textContent = '';
      var act = b.dataset.act;
      if (act === 'clear') buf = '';
      else if (act === 'del') buf = buf.slice(0, -1);
      else if (buf.length < MAX) buf += b.textContent.trim();
      renderDots();
    });
  });

  // Фізична клавіатура — цифри й Backspace дублюють keypad, Enter шле форму.
  document.addEventListener('keydown', function (e) {
    if (/^[0-9]$/.test(e.key)) { if (buf.length < MAX) { buf += e.key; renderDots(); } errEl.textContent = ''; }
    else if (e.key === 'Backspace') { buf = buf.slice(0, -1); renderDots(); }
    else if (e.key === 'Enter') { e.preventDefault(); if (buf) form.requestSubmit ? form.requestSubmit() : form.submit(); else shake(); }
  });

  // Порожній сабміт не має сенсу — сервер однаково відхилить, тож гасимо тут.
  form.addEventListener('submit', function (e) { if (!buf) { e.preventDefault(); shake(); } });

  renderDots();
  buildAndRun();

  // Помилка з сервера: keypad потрібен одразу, тож не чекаємо весь пробіг табло.
  if (form.dataset.pinError) { after.classList.add('show'); shake(); }
})();
