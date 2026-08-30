// Шестерня «вигляд списку» — спільний компонент черги і тріажу пошти.
//
// Один код на два екрани свідомо: правила однакові (числа в межах, нуль =
// «як було», пресет ставить набір одним рухом), а дві копії розійшлися б на
// першій же правці. Різницю задає розмітка через data-*, не форк файлу.
//
// Стан живе на АКАУНТІ оператора, не в localStorage: набір має їхати за
// людиною, а не за браузером, і повертатись при наступному вході. Сервер уже
// підставив стартові значення в data-* і в style — цей код нічого не
// «вмикає», лише крутить далі.
(function () {
  "use strict";

  // Ті самі межі, що в app/services/look_prefs.py. Дублюються свідомо: кнопка
  // мусить гаснути на краю ОДРАЗУ, а не після відповіді мережі. Розійтись
  // тихо вони не можуть — за цим стежить тест.
  var LIMITS = { pad: [2, 28], width: [340, 1180] };

  // Утримання кнопки. Перший крок — одразу по натисканню (клік має лишатись
  // клацанням), далі пауза, щоб звичайне клацання не перетворювалось на
  // серію, і тільки потім повтор із прискоренням: доводити 6px до 24px по
  // одному клацанню — це 9 клацань, а тримати кнопку 2 секунди — один рух.
  var HOLD_DELAY = 380;
  var HOLD_FAST = 45;
  var HOLD_SLOW = 110;
  var HOLD_RAMP = 900; // за скільки мс розгін доходить до максимуму

  function clamp(value, bounds) {
    return Math.max(bounds[0], Math.min(bounds[1], value));
  }

  function initGear(root) {
    var scope = root.dataset.lookScope;
    var host = document.querySelector(root.dataset.lookHost || "body");
    var panel = root.querySelector("[data-look-panel]");
    var toggle = root.querySelector("[data-look-toggle]");
    if (!scope || !host || !panel || !toggle) return;

    // Значення, з якого починає лічильник відступу, якщо оператор ще нічого
    // не крутив. Мусить збігатися з дефолтом CSS-змінної на цьому екрані.
    var padFallback = parseInt(root.dataset.lookPadDefault || "6", 10);

    var state = {
      pad: parseInt(root.dataset.pad || "0", 10) || 0,
      width: parseInt(root.dataset.width || "0", 10) || 0,
      density: root.dataset.density || "",
      matStyle: root.dataset.matStyle || "",
      step: parseInt(root.dataset.step || "2", 10) || 2,
    };

    var hasWidth = !!root.querySelector('[data-look-out="width"]');

    function panelWidthNow() {
      var target = document.querySelector(root.dataset.lookWidthTarget || "");
      return target ? Math.round(target.getBoundingClientRect().width) : 0;
    }

    function apply() {
      // Нуль — це «як було», тому змінну треба ЗНЯТИ, а не писати в неї
      // запасне число: інлайнові 8px перемагали і пресет «Компактний»
      // (той дає свої 4px запасним значенням), і власний дефолт вузького
      // режиму списку. Пресет мовчки нічого не робив.
      if (state.pad) host.style.setProperty("--look-row-pad", state.pad + "px");
      else host.style.removeProperty("--look-row-pad");
      if (hasWidth) {
        if (state.width) host.style.setProperty("--look-list-w", state.width + "px");
        else host.style.removeProperty("--look-list-w");
      }
      // Пресет щільності і вигляд колонки кольору лишаються АТРИБУТАМИ: вони
      // перемикають цілий набір токенів, а не одне число.
      if (state.density) host.setAttribute("data-density", state.density);
      else host.removeAttribute("data-density");
      if (state.matStyle) host.setAttribute("data-matstyle", state.matStyle);
      else host.removeAttribute("data-matstyle");
      render();
    }

    function render() {
      var padOut = root.querySelector('[data-look-out="pad"]');
      var widthOut = root.querySelector('[data-look-out="width"]');
      if (padOut) padOut.textContent = (state.pad || padFallback) + " px";
      if (widthOut) widthOut.textContent = state.width ? state.width + " px" : "авто";
      setPressed("[data-look-step]", "lookStep", String(state.step));
      // Пресет підсвічений, лише коли збігаються ОБИДВА його числа —
      // інакше після ручного доведення підсвіченим лишався б набір, від
      // якого на екрані вже нічого не лишилось.
      root.querySelectorAll("[data-look-preset]").forEach(function (btn) {
        var samePad = (parseInt(btn.dataset.lookPresetPad || "0", 10) || 0) === state.pad;
        btn.setAttribute("aria-pressed",
          String(btn.dataset.lookPreset === state.density && samePad));
      });
      setPressed("[data-look-mat]", "lookMat", state.matStyle);
      // Кнопка на краю діапазону гасне — інакше вона мовчки нічого не робить,
      // і оператор тисне далі, думаючи, що зламалось.
      ["pad", "width"].forEach(function (key) {
        var value = key === "pad" ? state.pad || padFallback : state.width || 0;
        var dec = root.querySelector('[data-look-dec="' + key + '"]');
        var inc = root.querySelector('[data-look-inc="' + key + '"]');
        if (dec) dec.disabled = value !== 0 && value <= LIMITS[key][0];
        if (inc) inc.disabled = value !== 0 && value >= LIMITS[key][1];
      });
    }

    function setPressed(selector, dataKey, value) {
      root.querySelectorAll(selector).forEach(function (btn) {
        btn.setAttribute("aria-pressed", String(btn.dataset[dataKey] === value));
      });
    }

    var saveTimer = null;
    function save() {
      // Серія клацань (а тим більше утримання) — це ОДНА зміна: чекаємо, поки
      // рука зупиниться, і пишемо один раз. Інакше секунда утримання давала б
      // два десятки запитів.
      window.clearTimeout(saveTimer);
      saveTimer = window.setTimeout(function () {
        var body = new URLSearchParams({
          scope: scope,
          row_pad: String(state.pad),
          list_width: String(state.width),
          density: state.density,
          mat_style: state.matStyle,
          step: String(state.step),
        });
        fetch("/account/look", {
          method: "POST",
          headers: { "Content-Type": "application/x-www-form-urlencoded" },
          body: body.toString(),
          credentials: "same-origin",
        })
          .then(function (response) {
            if (!response.ok) failed();
          })
          .catch(failed);
      }, 400);
    }

    function failed() {
      if (window.showToast) window.showToast("Не вдалось зберегти вигляд", "error");
    }

    function nudge(key, direction) {
      var base = key === "pad" ? state.pad || padFallback : state.width || panelWidthNow();
      var next = clamp(base + direction * state.step, LIMITS[key]);
      if (next === state[key]) return false;
      state[key] = next;
      apply();
      save();
      return true;
    }

    // ---- утримання ---------------------------------------------------------
    var hold = null;
    function stopHold() {
      if (!hold) return;
      window.clearTimeout(hold.timer);
      hold = null;
    }
    function startHold(key, direction) {
      stopHold();
      hold = { started: Date.now(), timer: null };
      var tick = function () {
        if (!hold) return;
        if (!nudge(key, direction)) { stopHold(); return; }
        var elapsed = Date.now() - hold.started - HOLD_DELAY;
        var ratio = Math.max(0, Math.min(1, elapsed / HOLD_RAMP));
        var wait = HOLD_SLOW - (HOLD_SLOW - HOLD_FAST) * ratio;
        hold.timer = window.setTimeout(tick, wait);
      };
      hold.timer = window.setTimeout(tick, HOLD_DELAY);
    }

    root.addEventListener("pointerdown", function (event) {
      var dec = event.target.closest("[data-look-dec]");
      var inc = dec ? null : event.target.closest("[data-look-inc]");
      var btn = dec || inc;
      if (!btn || btn.disabled) return;
      // Перший крок — на натисканні, далі повтор. Тому click на цих кнопках
      // НЕ обробляється: інакше відпускання давало б зайвий крок.
      event.preventDefault();
      var key = dec ? dec.dataset.lookDec : inc.dataset.lookInc;
      var direction = dec ? -1 : 1;
      if (nudge(key, direction)) startHold(key, direction);
      // Палець може з'їхати з кнопки — ловимо відпускання на вікні, інакше
      // лічильник крутився б далі вже без натиснутої кнопки.
      btn.setPointerCapture && btn.setPointerCapture(event.pointerId);
    });
    ["pointerup", "pointercancel", "blur"].forEach(function (name) {
      window.addEventListener(name, stopHold, true);
    });

    root.addEventListener("click", function (event) {
      var preset = event.target.closest("[data-look-preset]");
      if (preset) {
        // Пресет ставить набір одним рухом — і щільність, і відступ:
        // інакше «Компактний» лишався б із чужими 20px і виглядав зламаним.
        state.density = preset.dataset.lookPreset;
        state.pad = parseInt(preset.dataset.lookPresetPad || "0", 10) || 0;
        apply();
        save();
        return;
      }
      var mat = event.target.closest("[data-look-mat]");
      if (mat) {
        state.matStyle = mat.dataset.lookMat;
        apply();
        save();
        return;
      }
      var stepBtn = event.target.closest("[data-look-step]");
      if (stepBtn) {
        state.step = parseInt(stepBtn.dataset.lookStep, 10) || 2;
        render();
        save();
        return;
      }
      if (event.target.closest("[data-look-reset]")) {
        state.pad = 0;
        state.width = 0;
        state.density = "";
        state.matStyle = "";
        host.style.removeProperty("--look-row-pad");
        host.style.removeProperty("--look-list-w");
        apply();
        save();
        if (root.dataset.lookResetEvent) {
          document.dispatchEvent(new CustomEvent(root.dataset.lookResetEvent));
        }
        return;
      }
      if (event.target.closest("[data-look-toggle]")) {
        setOpen(panel.hidden);
      }
    });

    function setOpen(open) {
      panel.hidden = !open;
      toggle.setAttribute("aria-expanded", String(open));
    }

    // Клік повз панель і Esc закривають її — інакше вона накриває верх списку
    // доти, доки оператор не здогадається клацнути по шестерні ще раз.
    document.addEventListener("click", function (event) {
      if (panel.hidden || root.contains(event.target)) return;
      setOpen(false);
    });
    document.addEventListener("keydown", function (event) {
      if (event.key !== "Escape" || panel.hidden) return;
      setOpen(false);
      toggle.focus();
    });

    render();
  }

  document.querySelectorAll("[data-look-gear]").forEach(initGear);
})();
