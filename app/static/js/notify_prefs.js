// Налаштування спливаючих сповіщень: підсвітка обраного варіанта і живий
// показ того, що буде видно на робочому екрані.
//
// Чому окремий файл, а не settings_console.js. Розділ «Сповіщення» переїхав
// зі «Стенда» в кабінет (/account, вкладка «Сповіщення»), а обробник лишився
// в settings_console.js, який /account не вантажить. Наслідок був такий, що
// зовні виглядав як «кнопка не натискається»: радіо перемикалось, але клас
// .is-on ставив сервер лише на рендері, тож до збереження й перезавантаження
// сторінки НІЩО не змінювалось, а «Показати приклад» просто мовчав. Тепер
// поведінка живе поруч з єдиним екраном, який її показує.
//
// Прев'ю навмисне зроблене СПРАВЖНІМИ тостами, а не намальованим зразком:
// вигляд «Аврора» розводить повідомлення по трьох каналах (картка, нижня
// стрічка, кромка), і намальована картинка показала б лише один з них —
// тобто збрехала б рівно там, де вибір найважчий.
(function () {
  var styles = document.querySelector(".notify-styles");
  var positions = document.querySelector(".notify-pos");
  if (!styles && !positions) return;

  var stack = document.getElementById("toast-stack");

  // Кожен приклад несе ключ події, бо саме він визначає канал «Аврори»:
  // збій іде в кромку, подія ззовні — карткою, підтвердження власної дії —
  // нижньою стрічкою. Без ключа проба показувала б один канал із трьох.
  var SAMPLES = [
    ["2 роботи можна брати — технік доклав шлях до папки.", "info", "ready_to_take"],
    ["Технік змінив роботу в таблиці. Позначені в черзі — перевірте перед фрезеруванням.", "warning", "sheet_changed"],
    ["Статус → відфрезеровано · наряд 24122", "success", null],
    ["Google Таблиця не відповідає. Черга не оновлюється — перевірте зʼєднання.", "error", "sheet_error"],
    ["Синхронізація відновлена.", "success", "sheet_recovered"],
  ];
  var next = 0;

  // Збережений стан — те, з чим сторінка прийшла з сервера. Прев'ю тимчасово
  // підміняє його на контейнері тостів і мусить повернути назад: інакше
  // фонова подія (синк, пошта) прилетіла б у вигляді, який людина лише
  // приміряла й не зберегла.
  var saved = {
    style: (stack && stack.dataset.toastStyle) || "glass",
    pos: (stack && stack.dataset.toastPos) || "tc",
  };
  // 7000 мс — найдовший тост (TOAST_LIFE у app.js) плюс запас на анімацію.
  var HOLD_MS = 8000;
  var holdTimer = null;

  function picked(group, fallback) {
    var on = group && group.querySelector('input[type="radio"]:checked');
    return (on && on.value) || fallback;
  }

  function restore() {
    if (!stack) return;
    stack.dataset.toastStyle = saved.style;
    stack.dataset.toastPos = saved.pos;
  }

  // Показати приклад так, як він виглядатиме після збереження: контейнер
  // отримує ОБРАНІ (ще не збережені) вигляд і позицію, тост малюється тими
  // самими правилами, що й у роботі, а потім усе повертається на збережене.
  function preview(sample) {
    if (!stack || !window.showToast) return;
    stack.dataset.toastStyle = picked(styles, saved.style);
    stack.dataset.toastPos = picked(positions, saved.pos);
    window.showToast(sample[0], sample[1], undefined, undefined, sample[2] ? { event: sample[2] } : undefined);
    if (holdTimer) window.clearTimeout(holdTimer);
    holdTimer = window.setTimeout(restore, HOLD_MS);
  }

  // Вибір вигляду показує подію ЗЗОВНІ (картка в усіх трьох виглядах) —
  // її видно однаково, тож варіанти можна порівняти між собою. Вибір позиції
  // показує підтвердження власної дії: воно коротке й не перекриває екран,
  // а питання там саме «звідки воно вилізе».
  var STYLE_SAMPLE = SAMPLES[0];
  var POS_SAMPLE = SAMPLES[2];

  document.querySelectorAll('.notify-pick input[type="radio"]').forEach(function (input) {
    input.addEventListener("change", function () {
      var group = input.closest(".notify-styles, .notify-pos");
      if (!group) return;
      group.querySelectorAll(".notify-pick").forEach(function (p) {
        p.classList.toggle("is-on", p.contains(input) && input.checked);
      });
      preview(group === positions ? POS_SAMPLE : STYLE_SAMPLE);
    });
  });

  // «Показати приклад» перебирає всі п'ять типів подій по черзі — щоб було
  // видно не лише картку, а й стрічку з кромкою, коли обрана «Аврора».
  var btn = document.querySelector("[data-notify-preview]");
  if (btn) {
    btn.addEventListener("click", function () {
      preview(SAMPLES[next++ % SAMPLES.length]);
    });
  }
})();

// ── Спливаюче вікно KuubMill (05.10.26) ───────────────────────────────────
// Кожна зміна одразу зберігається (POST /settings/desktop-popup, увесь стан
// форми — сервер читає відсутню галочку як «знято»). Пробне вікно й режим
// «Налаштувати положення» — окремі POST; саме вікно малює програма KuubMill.
(function () {
  var root = document.querySelector("[data-dn-root]");
  var form = root && root.querySelector("[data-dn-form]");
  if (!form) return;
  var saved = root.querySelector("[data-dn-saved]");
  var range = root.querySelector("[data-dn-secs]");
  var label = root.querySelector("[data-dn-secs-label]");
  var until = root.querySelector("[data-dn-until]");
  var savedTimer = null;

  function toast(msg, kind) {
    if (window.showToast) window.showToast(msg, kind || "info");
  }

  function post(url, data) {
    return fetch(url, { method: "POST", body: data, headers: { "X-Requested-With": "fetch" } })
      .then(function (r) {
        if (r.ok) return r;
        return r.json().catch(function () { return {}; }).then(function (j) {
          throw new Error(j.detail || ("HTTP " + r.status));
        });
      });
  }

  function save() {
    post("/settings/desktop-popup", new FormData(form)).then(function () {
      saved.hidden = false;
      window.clearTimeout(savedTimer);
      savedTimer = window.setTimeout(function () { saved.hidden = true; }, 1500);
    }).catch(function (e) { toast("Не збережено: " + e.message, "error"); });
  }

  function syncSeconds() {
    range.disabled = until.checked;
    label.textContent = until.checked ? "до кліку" : range.value + " с";
  }

  function syncSpots() {
    var picked = form.querySelector('input[name="anchor"]:checked');
    root.querySelectorAll("[data-spot]").forEach(function (spot) {
      spot.classList.toggle("is-on", !!picked && spot.dataset.spot === picked.value);
    });
  }

  root.addEventListener("change", function (e) {
    if (e.target === until) syncSeconds();
    if (e.target.name === "anchor") syncSpots();
    save();
  });
  range.addEventListener("input", syncSeconds);

  root.querySelectorAll("[data-spot]").forEach(function (spot) {
    spot.addEventListener("click", function () {
      var radio = form.querySelector('input[name="anchor"][value="' + spot.dataset.spot + '"]');
      if (!radio || radio.disabled || form.inert) return;
      radio.checked = true;
      syncSpots();
      save();
    });
  });

  root.querySelector("[data-dn-test]").addEventListener("click", function () {
    var data = new FormData();
    var lab = form.querySelector('input[name="events"][value="lab"]');
    data.append("kind", lab && !lab.checked ? "mail" : "lab");
    post("/settings/desktop-popup/test", data)
      .then(function () { toast("Пробне вікно — у вибраному місці екрана."); })
      .catch(function (e) { toast(e.message, "error"); });
  });

  root.querySelector("[data-dn-place]").addEventListener("click", function () {
    post("/settings/desktop-popup/place", new FormData())
      .then(function () { toast("Перетягни вікно-зразок мишею й натисни «Зберегти тут». Після цього тут з'явиться «Своє місце».", "info"); })
      .catch(function (e) { toast(e.message, "error"); });
  });
})();
