// Видача (CLAUDE.md §9.4).
//
// Тут лише перемикач «Плитки / Рядки» і його відновлення. Головне: відмітка
// «знайдено» підмінює список карток через HTMX, а клас режиму живе на самому
// списку — тому після кожної підміни режим треба ставити наново, інакше
// галочка мовчки скидала «Плитки» на «Рядки».

// Handout view switch: "Рядки" (compact list, default) ⇄ "Плитки" (preview
// tiles). Same markup, two CSS layouts (.clients / .clients.as-tiles). The
// choice is remembered per-browser in localStorage so it survives navigation
// and reloads. No-op on every other screen.
const HANDOUT_VIEW_KEY = "handout-view";

function applyHandoutView(mode) {
  const root = document.querySelector("[data-view-root]");
  if (!root) return;
  const tiles = mode === "tiles";
  root.classList.toggle("as-tiles", tiles);
  document.querySelectorAll(".view-btn").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.viewMode === (tiles ? "tiles" : "rows"));
  });
}

document.addEventListener("click", (event) => {
  const btn = event.target.closest(".view-btn");
  if (!btn) return;
  const mode = btn.dataset.viewMode === "tiles" ? "tiles" : "rows";
  try {
    window.localStorage.setItem(HANDOUT_VIEW_KEY, mode);
  } catch (_) {
    /* private mode / storage disabled — the toggle still works this session */
  }
  applyHandoutView(mode);
});

function restoreHandoutView() {
  if (!document.querySelector("[data-view-root]")) return;
  let saved = "rows";
  try {
    saved = window.localStorage.getItem(HANDOUT_VIEW_KEY) || "rows";
  } catch (_) {
    /* ignore */
  }
  applyHandoutView(saved);
}

document.addEventListener("DOMContentLoaded", restoreHandoutView);

// Відмітка «знайдено» підмінює список карток через HTMX, а `as-tiles` живе на
// самому списку — без цього кожна галочка мовчки скидала «Плитки» на «Рядки».
document.body.addEventListener("htmx:afterSwap", (event) => {
  if (event.target && event.target.id === "handout-list") restoreHandoutView();
});
