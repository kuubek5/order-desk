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
    var ls = storage();
    if (!ls) return;
    try {
      ls.setItem(key(name), String(value));
    } catch (e) {
      /* сховище недоступне або повне — просто не запам'ятаємо цю сесію */
    }
  }

  function remove(name) {
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

  window.KMStore = {
    PREFIX: PREFIX,
    key: key,
    get: get,
    set: set,
    remove: remove,
  };
})();
