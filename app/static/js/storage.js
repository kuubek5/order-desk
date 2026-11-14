/*
 * Єдиний вхід до localStorage: KMStore.
 *
 * ЧОМУ ЦЕ ІСНУЄ. Настройки браузера («рейка згорнута», «плитки чи рядки»,
 * ширини стовпців, згорнуті картки видачі, швидкість обертання STL) писались
 * рядковими літералами без спільного простору імен: railCollapsed,
 * handout-view, stl-preview-max, handout:collapsed:<клієнт>:<день>… Це давало
 * дві проблеми. Перша — сусідство: сторінка ділить localStorage з усім, що
 * колись жило на 127.0.0.1 (dev-сервер, старі інструменти), тож короткі імена
 * на кшталт accTab легко зіштовхнути. Друга й важливіша — версія: коли формат
 * збереженого значення зміниться, стара вкладка підставить несумісне значення
 * мовчки, і зневадити це нема за чим. Префікс `kuubmill:v1:` вирішує обидві:
 * простір імен наш, а «v1» дає куди переїхати без гри в «а що там лежить».
 *
 * ЧОМУ ОДНА ФУНКЦІЯ, А НЕ ПРЕФІКС У КОЖНОМУ ФАЙЛІ. Літерал, скопійований у
 * вісім файлів, розходиться на першій же правці — і половина настройок тихо
 * «зникає». Тут ключ будує рівно одне місце: KMStore.key().
 *
 * ЧОМУ БЕЗ defer. Анти-мигтіння в base.html (клас на <body> ДО першого
 * малювання) — інлайн-скрипт у тілі документа, він виконується раніше за
 * будь-який defer. Щоб і він ходив через ту саму функцію, а не тримав
 * власну копію префікса, цей файл підключений блокуюче й першим.
 */
(function () {
  "use strict";

  var PREFIX = "kuubmill:v1:";

  // Ключі, які писались БЕЗ префікса (до 06.09.26). Список потрібен лише
  // міграції — нового коду, що їх пише, вже немає.
  var LEGACY_KEYS = [
    "railCollapsed",
    "layoutEditMode",
    "widgetEditMode",
    "furnaceSideOpen",
    "machineSideOpen",
    "qsideCollapsed",
    "queueColWidths",
    "handout-view",
    "mailSyncAt",
    "accTab",
    "stl-preview-max",
    "stl-preview-spin-speed",
    "stl-gallery-max",
  ];

  // Динамічні ключі: `handout:collapsed:<клієнт>:<день>` — по одному на
  // згорнуту картку, імена наперед невідомі, тому переносимо за префіксом.
  var LEGACY_PREFIXES = ["handout:collapsed:"];

  function storage() {
    // Приватний режим і вимкнене сховище кидають уже на доступі до властивості.
    try {
      return window.localStorage || null;
    } catch (e) {
      return null;
    }
  }

  function key(name) {
    return name.indexOf(PREFIX) === 0 ? name : PREFIX + name;
  }

  function get(name) {
    var ls = storage();
    if (!ls) return null;
    try {
      return ls.getItem(key(name));
    } catch (e) {
      return null;
    }
  }

  function set(name, value) {
    queueSync(name, String(value));
    var ls = storage();
    if (!ls) return;
    try {
      ls.setItem(key(name), String(value));
    } catch (e) {
      /* сховище недоступне або повне — просто не запам'ятаємо цю сесію */
    }
  }

  function remove(name) {
    queueSync(name, null);
    var ls = storage();
    if (!ls) return;
    try {
      ls.removeItem(key(name));
    } catch (e) {
      /* нічого страшного: значення й далі читатиметься як «не задано» */
    }
  }

  function isLegacy(k) {
    if (!k || k.indexOf(PREFIX) === 0) return false;
    if (LEGACY_KEYS.indexOf(k) !== -1) return true;
    for (var i = 0; i < LEGACY_PREFIXES.length; i++) {
      if (k.indexOf(LEGACY_PREFIXES[i]) === 0) return true;
    }
    return false;
  }

  // Одноразовий перенос старих ключів. Без нього оператор, який уже згорнув
  // рейку й розклав ширини стовпців під свій монітор, після оновлення побачив
  // би все скинутим — і без жодного повідомлення, що саме сталось.
  //
  // Ідемпотентність без окремого прапорця «мігровано»: старий ключ після
  // переносу видаляється, тож наступний запуск просто нічого не знаходить.
  // Значення під НОВИМ ключем ніколи не затирається старим — якщо людина вже
  // встигла клікнути в новій версії, її вибір новіший за спадок.
  function migrate() {
    var ls = storage();
    if (!ls) return;
    var stale = [];
    try {
      // Спершу зібрати, потім міняти: видалення під час обходу зсуває індекси.
      for (var i = 0; i < ls.length; i++) {
        var k = ls.key(i);
        if (isLegacy(k)) stale.push(k);
      }
    } catch (e) {
      return;
    }
    for (var j = 0; j < stale.length; j++) {
      var old = stale[j];
      try {
        var value = ls.getItem(old);
        if (value !== null && ls.getItem(PREFIX + old) === null) {
          ls.setItem(PREFIX + old, value);
        }
        ls.removeItem(old);
      } catch (e) {
        /* не вдалось перенести один ключ — решта переносу не зупиняється */
      }
    }
  }

  migrate();

  // ── Памʼять акаунта (власник 14.11.26) ─────────────────────────────────
  // Ці ключі — налаштування оператора, не браузера: сервер віддає їх у
  // `window.KM_ACCOUNT_STORE` (base.html, ДО цього файлу), і значення акаунта
  // перемагає локальне — на іншому ПК оператор бачить своє. Перелік мусить
  // збігатися з `app/services/account_store.py::SYNCED_KEYS` (сторож —
  // tests/test_account_store.py).
  var SYNCED = [
    "railCollapsed",
    "sismaOpen",
    "queueColWidths",
    "furnaceSideOpen",
    "machineSideOpen",
    "qsideCollapsed",
    "statsDayView",
    "accTab",
    "discsTab",
    "mail.attlistWide",
    "mailMirrorCollapsed",
    "mail.showAll",
    "stl-preview-max",
    "stl-preview-light",
    "stl-preview-spin-speed",
    "stl-gallery-max",
  ];
  var account = window.KM_ACCOUNT_STORE;
  var signedIn = !!account && typeof account === "object";
  var pending = {};
  var timer = null;

  function isSynced(name) {
    return SYNCED.indexOf(name.indexOf(PREFIX) === 0 ? name.slice(PREFIX.length) : name) !== -1;
  }

  function flush() {
    timer = null;
    var batch = pending;
    pending = {};
    Object.keys(batch).forEach(function (name) {
      var body = new URLSearchParams();
      body.set("key", name);
      if (batch[name] === null) body.set("clear", "1");
      else body.set("value", batch[name]);
      try {
        fetch("/account/ui-store", {
          method: "POST",
          headers: { "Content-Type": "application/x-www-form-urlencoded" },
          body: body.toString(),
          credentials: "same-origin",
          keepalive: true,
        }).catch(function () {
          /* мережа впала — локально значення вже є, акаунт наздожене наступна зміна */
        });
      } catch (e) {
        /* fetch недоступний — лишається локальне значення, як до 14.11.26 */
      }
    });
  }

  function queueSync(name, value) {
    if (!signedIn || !isSynced(name)) return;
    var bare = name.indexOf(PREFIX) === 0 ? name.slice(PREFIX.length) : name;
    pending[bare] = value;
    // Перетягування межі стовпця пише ширини на кожен рух — шлемо одне в кінці.
    if (timer) clearTimeout(timer);
    timer = setTimeout(flush, 600);
  }

  // Акаунт → браузер: значення акаунта перемагає. Ключ, якого в акаунті ще
  // немає, але є в цьому браузері (налаштовано до 14.11.26), — один раз
  // відвантажити в акаунт, щоб після оновлення оператор не побачив усе скинутим.
  function adoptAccount() {
    var ls = storage();
    if (!ls || !signedIn) return;
    SYNCED.forEach(function (name) {
      try {
        if (Object.prototype.hasOwnProperty.call(account, name)) {
          ls.setItem(key(name), String(account[name]));
        } else {
          var local = ls.getItem(key(name));
          if (local !== null) queueSync(name, local);
        }
      } catch (e) {
        /* один ключ не вдався — решта не зупиняється */
      }
    });
  }

  adoptAccount();

  window.KMStore = {
    PREFIX: PREFIX,
    key: key,
    get: get,
    set: set,
    remove: remove,
  };
})();
