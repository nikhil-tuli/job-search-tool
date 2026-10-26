// Show one section of the Config page, or all. The choice is remembered in this browser.
// Hidden sections stay in the form, so saving still sends every setting.
(function () {
  var pick = document.getElementById("cfg-pick");
  if (!pick) return;
  var KEY = "jobscan_config_view";
  var buttons = pick.querySelectorAll("button");
  var sections = document.querySelectorAll(".cfg-section");
  function apply(view) {
    sections.forEach(function (s) {
      s.hidden = view !== "all" && s.getAttribute("data-section") !== view;
    });
    buttons.forEach(function (b) {
      b.classList.toggle("on", b.getAttribute("data-view") === view);
    });
  }
  var saved = "all";
  try { saved = localStorage.getItem(KEY) || "all"; } catch (e) {}
  if (!pick.querySelector('[data-view="' + saved + '"]')) saved = "all";
  pick.hidden = false;
  apply(saved);
  buttons.forEach(function (b) {
    b.addEventListener("click", function () {
      var view = b.getAttribute("data-view");
      try { localStorage.setItem(KEY, view); } catch (e) {}
      apply(view);
    });
  });
})();
