// User menu: close on outside click or Escape.
document.addEventListener("click", (e) =>
  document.querySelectorAll("details.menu[open]").forEach((d) => {
    if (!d.contains(e.target)) d.open = false;
  }),
);
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape")
    document
      .querySelectorAll("details.menu[open]")
      .forEach((d) => (d.open = false));
});

// Game autocomplete (nav search and the Add game form).
document.querySelectorAll("input[data-suggest]").forEach(function (input) {
  var mode = input.dataset.suggest,
    box = input.parentElement;
  var hidden = input.dataset.igdbField
    ? document.getElementById(input.dataset.igdbField)
    : null;
  var list = document.createElement("ul");
  list.className = "ac-list";
  list.hidden = true;
  box.appendChild(list);
  var items = [],
    active = -1,
    timer = null,
    seq = 0;
  function close() {
    list.hidden = true;
    active = -1;
  }
  function choose(game) {
    if (mode === "go") {
      location.href = game.id
        ? "/game/" + game.id
        : "/game/igdb/" + game.igdb_id;
    } else {
      input.value = game.title;
      if (hidden) hidden.value = game.igdb_id || "";
      close();
    }
  }
  function render() {
    list.textContent = "";
    items.forEach(function (game, i) {
      var li = document.createElement("li");
      if (i === active) li.className = "active";
      var lead = document.createElement("span");
      lead.className = "lead";
      if (game.cover_url) {
        var img = document.createElement("img");
        img.className = "thumb";
        img.alt = "";
        img.referrerPolicy = "no-referrer";
        img.setAttribute("data-cover", "");
        img.src = game.cover_url;
        lead.appendChild(img);
      }
      var t = document.createElement("span");
      t.textContent = game.title + (game.year ? " (" + game.year + ")" : "");
      lead.appendChild(t);
      li.appendChild(lead);
      if (game.n) {
        var m = document.createElement("span");
        m.className = "meta";
        m.textContent = game.avg.toFixed(1);
        li.appendChild(m);
      }
      li.addEventListener("mousedown", function (e) {
        e.preventDefault();
        choose(game);
      });
      list.appendChild(li);
    });
    list.hidden = !items.length;
  }
  input.addEventListener("input", function () {
    if (hidden) hidden.value = "";
    clearTimeout(timer);
    var q = input.value.trim();
    if (!q) {
      items = [];
      close();
      return;
    }
    timer = setTimeout(function () {
      var mine = ++seq;
      fetch("/api/games/suggest?q=" + encodeURIComponent(q))
        .then(function (r) {
          return r.ok ? r.json() : [];
        })
        .then(function (data) {
          if (mine !== seq) return;
          items = data;
          active = -1;
          render();
        })
        .catch(function () {});
    }, 250);
  });
  input.addEventListener("keydown", function (e) {
    if (list.hidden || !items.length) return;
    if (e.key === "ArrowDown") {
      e.preventDefault();
      active = (active + 1) % items.length;
      render();
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      active = (active - 1 + items.length) % items.length;
      render();
    } else if (e.key === "Enter" && active >= 0) {
      e.preventDefault();
      choose(items[active]);
    } else if (e.key === "Escape") {
      close();
    }
  });
  input.addEventListener("blur", close);
});

// Selects marked data-autosubmit save as soon as you pick an option.
document.querySelectorAll("select[data-autosubmit]").forEach(function (select) {
  select.addEventListener("change", function () {
    select.form.submit();
  });
});
