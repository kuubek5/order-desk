// Вкладки сторінки «Бібліотека матеріалів»: «Розпізнавання» / «Скорочення».
// Прогресивне покращення — вкладки це <a> з реальними href, тож без JS вони
// перемикаються перезавантаженням, а сервер уже рендерить активну панель. Тут
// лише миттєве переключення без перезавантаження + оновлення адреси, щоб
// редіректи форм (…?tab=shortcuts) і перезавантаження лишались на місці.
(function () {
  "use strict";
  function show(key) {
    document.querySelectorAll(".ml-tab").forEach(function (b) {
      b.classList.toggle("is-on", b.dataset.mattab === key);
    });
    document.querySelectorAll("[data-mattab-pane]").forEach(function (p) {
      p.classList.toggle("is-on", p.dataset.mattabPane === key);
    });
  }
  document.addEventListener("click", function (e) {
    var tab = e.target.closest ? e.target.closest(".ml-tab") : null;
    if (!tab) return;
    e.preventDefault();
    show(tab.dataset.mattab);
    if (history.replaceState) history.replaceState(null, "", tab.getAttribute("href"));
  });
})();
