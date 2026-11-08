// Retry cover images that fail to load, then hide them cleanly.
document.addEventListener(
  "error",
  function (e) {
    var img = e.target;
    if (!img || img.tagName !== "IMG" || !img.hasAttribute("data-cover"))
      return;
    var n = +(img.getAttribute("data-tries") || 0);
    if (n >= 2) {
      img.classList.add("failed");
      return;
    }
    img.setAttribute("data-tries", n + 1);
    var base = img.getAttribute("data-base");
    if (!base) {
      base = img.getAttribute("src").split("?")[0];
      img.setAttribute("data-base", base);
    }
    setTimeout(
      function () {
        img.src = n === 0 ? base : base + "?r=" + (n + 1);
      },
      800 * (n + 1),
    );
  },
  true,
);
