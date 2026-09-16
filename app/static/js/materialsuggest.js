// Combobox for the manual add-work material field (WAI-ARIA combobox 1.2).
//
// One delegated controller on document, so it covers every add-work row —
// including the ones "+ ще рядок" clones after this script ran (queue.js clears
// the clone's list and combobox state; assigning option ids lazily here keeps
// them unique across rows). The field stays free text: nothing is forced on
// blur, an unmatched value is left exactly as typed (owner decision 15.09.26).
//
// Server returns the option list as an HTMX fragment; we drive it with fetch
// rather than hx-attributes because the rows are cloned and per-row targets are
// awkward to rewire — one mechanism, one list, still server-rendered.
(function () {
  "use strict";
  var DEBOUNCE = 150;
  var seq = 0;
  var timers = new WeakMap();

  function listFor(input) {
    return input.parentElement
      ? input.parentElement.querySelector("[data-matsuggest-list]")
      : null;
  }
  function inputFor(ul) {
    return ul.parentElement
      ? ul.parentElement.querySelector("[data-matsuggest-input]")
      : null;
  }
  function endpointFor(input) {
    // Поле матеріалу й поле клієнта ділять цей контролер; ендпоінт бере з
    // обгортки [data-matsuggest], дефолт — матеріал.
    var box = input.closest ? input.closest("[data-matsuggest]") : null;
    return (box && box.getAttribute("data-suggest-url")) || "/suggest/material";
  }
  function activeOpt(ul) {
    return ul.querySelector(".ms-opt.is-active");
  }
  function clearActive(ul) {
    var a = activeOpt(ul);
    if (a) a.classList.remove("is-active");
  }
  function setActive(input, ul, li) {
    clearActive(ul);
    if (!li) {
      input.removeAttribute("aria-activedescendant");
      return;
    }
    li.classList.add("is-active");
    input.setAttribute("aria-activedescendant", li.id);
    li.scrollIntoView({ block: "nearest" });
  }
  function close(input) {
    var ul = listFor(input);
    if (!ul) return;
    ul.hidden = true;
    input.setAttribute("aria-expanded", "false");
    input.removeAttribute("aria-activedescendant");
    clearActive(ul);
  }
  function assignIds(input, ul) {
    if (!ul.id) ul.id = "ms-list-" + ++seq;
    input.setAttribute("aria-controls", ul.id);
    var opts = ul.children;
    for (var i = 0; i < opts.length; i++) opts[i].id = ul.id + "-opt-" + i;
  }

  function query(input) {
    var ul = listFor(input);
    if (!ul) return;
    var q = input.value.trim();
    if (!q) {
      ul.innerHTML = "";
      close(input);
      return;
    }
    fetch(endpointFor(input) + "?q=" + encodeURIComponent(q), {
      headers: { "HX-Request": "true" },
    })
      .then(function (res) {
        return res.ok ? res.text() : null;
      })
      .then(function (html) {
        if (html === null) {
          close(input);
          return;
        }
        // Discard a stale response if the field moved on while it was in flight.
        if (input.value.trim() !== q) return;
        ul.innerHTML = html.trim();
        if (!ul.children.length) {
          close(input);
          return;
        }
        assignIds(input, ul);
        ul.hidden = false;
        input.setAttribute("aria-expanded", "true");
        setActive(input, ul, null);
      })
      .catch(function () {
        close(input);
      });
  }

  function expandWord(input, li) {
    // Shortcut expands ONLY the first token (the material word); the colour the
    // operator already typed after it is kept verbatim.
    var expansion = li.getAttribute("data-value");
    var m = input.value.match(/^(\s*)(\S+)([\s\S]*)$/);
    input.value = m ? m[1] + expansion + m[3] : expansion;
  }
  function choose(input, li) {
    if (!li) return false;
    if (li.getAttribute("data-kind") === "shortcut") expandWord(input, li);
    else input.value = li.getAttribute("data-value");
    close(input);
    input.focus();
    return true;
  }

  document.addEventListener("input", function (e) {
    var input = e.target.closest && e.target.closest("[data-matsuggest-input]");
    if (!input) return;
    clearTimeout(timers.get(input));
    timers.set(
      input,
      setTimeout(function () {
        query(input);
      }, DEBOUNCE)
    );
  });

  document.addEventListener("keydown", function (e) {
    var input = e.target.closest && e.target.closest("[data-matsuggest-input]");
    if (!input) return;
    var ul = listFor(input);
    if (!ul) return;
    var open = !ul.hidden && ul.children.length;

    if (e.key === "ArrowDown") {
      if (!open) {
        query(input);
        e.preventDefault();
        return;
      }
      var cur = activeOpt(ul);
      var next = cur ? cur.nextElementSibling : ul.firstElementChild;
      setActive(input, ul, next || ul.firstElementChild);
      e.preventDefault();
    } else if (e.key === "ArrowUp") {
      if (!open) return;
      var c2 = activeOpt(ul);
      var prev = c2 ? c2.previousElementSibling : ul.lastElementChild;
      setActive(input, ul, prev || ul.lastElementChild);
      e.preventDefault();
    } else if (e.key === "Enter") {
      if (open && activeOpt(ul) && choose(input, activeOpt(ul))) e.preventDefault();
      // No active option → let the form submit as usual.
    } else if (e.key === "Escape") {
      if (open) {
        close(input);
        e.preventDefault();
      }
    } else if (e.key === "Tab") {
      if (!open) return;
      var active = activeOpt(ul);
      if (active) {
        choose(input, active);
        e.preventDefault();
        return;
      }
      // Unique shortcut → expand its WORD (keep the typed colour). Uniqueness is
      // about the shortcut, not the whole list: the server marks the sole
      // expandable option data-expand="1", frecency rows sit alongside it.
      var expandables = ul.querySelectorAll('.ms-opt[data-expand="1"]');
      if (expandables.length === 1) {
        choose(input, expandables[0]);
        e.preventDefault();
        return;
      }
      // Otherwise Tab takes the top FULL spelling (frecency): the first option
      // that isn't a shortcut. Leading shortcut rows may sit above it when the
      // shortcut match is ambiguous (no data-expand) — we skip past them rather
      // than guess which shortcut to expand, but still accept the best full
      // suggestion below. Works for materials with the colour typed and for
      // clients (which have no shortcuts at all).
      var opts = ul.children;
      for (var i = 0; i < opts.length; i++) {
        if (opts[i].getAttribute("data-kind") !== "shortcut") {
          choose(input, opts[i]);
          e.preventDefault();
          return;
        }
      }
      close(input);
    }
  });

  // Mouse selects; hover is styled by CSS only, so it never steals the keyboard
  // caret's active option. mousedown (not click) so focus stays in the field.
  document.addEventListener("mousedown", function (e) {
    var li = e.target.closest && e.target.closest(".ms-opt");
    if (!li) return;
    var ul = li.closest("[data-matsuggest-list]");
    if (!ul) return;
    var input = inputFor(ul);
    if (!input) return;
    e.preventDefault();
    choose(input, li);
  });

  document.addEventListener("focusout", function (e) {
    var input = e.target.closest && e.target.closest("[data-matsuggest-input]");
    if (!input) return;
    setTimeout(function () {
      if (document.activeElement !== input) close(input);
    }, 120);
  });
})();
