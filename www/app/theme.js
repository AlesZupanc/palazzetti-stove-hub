// Applies the theme before the page is drawn (no flash). Dark is the default;
// "light" and "system" (follow the phone) are stored when picked in the app.
(function () {
  var t = "dark";
  try { t = localStorage.getItem("stove-theme") || "dark"; } catch (e) { /* storage blocked */ }
  if (t !== "light" && t !== "system") t = "dark";
  var dark = t === "dark" || (t === "system" && window.matchMedia && matchMedia("(prefers-color-scheme: dark)").matches);
  if (t !== "system") document.documentElement.setAttribute("data-theme", t);
  var meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute("content", dark ? "#12100e" : "#f5f1ec");
})();
