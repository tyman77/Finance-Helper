// Filter toolbar: show/hide rows by their data-status, purely client-side.
document.querySelectorAll(".filter-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".filter-btn").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    const filter = btn.dataset.filter;
    document.querySelectorAll("tbody tr").forEach((tr) => {
      if (tr.classList.contains("bill-head")) { tr.style.display = ""; return; }
      if (tr.classList.contains("grp-head")) {
        const sts = (tr.dataset.statuses || "").split(" ");
        tr.style.display = filter === "all" || sts.includes(filter) ? "" : "none";
        return;
      }
      tr.style.display = filter === "all" || tr.dataset.status === filter ? "" : "none";
    });
  });
});

// Candidate chips: fill the project field for that row instead of retyping a code.
document.querySelectorAll(".chip").forEach((chip) => {
  chip.addEventListener("click", () => {
    const field = document.getElementsByName(chip.dataset.fill)[0];
    if (field) field.value = chip.dataset.value;
  });
});

// Select/clear the post-checkbox on every row the current filter shows —
// "filter to Auto-coded, Select shown, Approve & Post selected" is the flow.
function setVisibleChecks(on) {
  document.querySelectorAll("tr[data-status]").forEach((tr) => {
    if (tr.style.display === "none") return;
    const box = tr.querySelector('input[type="checkbox"][name^="post_"]');
    if (box) box.checked = on;
  });
}
const selAll = document.getElementById("select-visible");
const clrAll = document.getElementById("clear-visible");
if (selAll) selAll.addEventListener("click", () => setVisibleChecks(true));
if (clrAll) clrAll.addEventListener("click", () => setVisibleChecks(false));

// Shift-click range selection: click one checkbox, shift-click another,
// and every visible row between them takes the second click's state.
(() => {
  const boxes = () => Array.from(
    document.querySelectorAll("tr[data-status]"))
    .filter((tr) => tr.style.display !== "none")
    .map((tr) => tr.querySelector('input[type="checkbox"][name^="post_"]'))
    .filter(Boolean);
  let last = null;
  document.addEventListener("click", (ev) => {
    const box = ev.target;
    if (!(box instanceof HTMLInputElement) || box.type !== "checkbox"
        || !box.name.startsWith("post_")) return;
    const list = boxes();
    if (ev.shiftKey && last && list.includes(last)) {
      const a = list.indexOf(last);
      const b = list.indexOf(box);
      for (let i = Math.min(a, b); i <= Math.max(a, b); i++) {
        list[i].checked = box.checked;
      }
    }
    last = box;
  });
})();

// Project + GL autocomplete: the native <datalist> popup is only as wide as
// the input and truncates names, so these fields get a custom dropdown —
// full names shown, searchable by code OR name ("amarillo" finds 2339,
// "hotel" finds 52300).
function codeAutocomplete(dataId, selector) {
  const el = document.getElementById(dataId);
  if (!el) return;
  let projects = [];
  try { projects = JSON.parse(el.textContent) || []; } catch (e) { return; }
  if (!projects.length) return;

  const panel = document.createElement("div");
  panel.className = "ac-panel";
  panel.hidden = true;
  document.body.appendChild(panel);
  let current = null;   // the input the panel is open for
  let active = -1;      // keyboard-highlighted row

  function close() { panel.hidden = true; current = null; active = -1; }

  function pick(code) {
    if (current) {
      current.value = code;
      current.dispatchEvent(new Event("change", { bubbles: true }));
    }
    close();
  }

  function render(input, onFocus) {
    let q = input.value.trim().toLowerCase();
    // Focusing a filled field shows the whole list, not just its own code.
    if (onFocus && projects.some((p) => p.c.toLowerCase() === q)) q = "";
    const hits = projects.filter((p) =>
      !q || p.c.startsWith(q) || p.n.toLowerCase().includes(q)).slice(0, q ? 12 : 60);
    if (!hits.length) { close(); return; }
    panel.textContent = "";
    hits.forEach((p, i) => {
      const row = document.createElement("div");
      row.className = "ac-item";
      const code = document.createElement("b");
      code.textContent = p.c;
      row.appendChild(code);
      row.appendChild(document.createTextNode(p.n ? " — " + p.n : ""));
      // mousedown, not click: it fires before the input's blur closes us.
      row.addEventListener("mousedown", (ev) => { ev.preventDefault(); pick(p.c); });
      panel.appendChild(row);
    });
    const r = input.getBoundingClientRect();
    panel.style.left = `${r.left + window.scrollX}px`;
    panel.style.top = `${r.bottom + window.scrollY + 2}px`;
    panel.style.minWidth = `${Math.max(r.width, 340)}px`;
    panel.hidden = false;
    current = input;
    active = -1;
  }

  function highlight(delta) {
    const rows = Array.from(panel.children);
    if (!rows.length) return;
    active = (active + delta + rows.length) % rows.length;
    rows.forEach((row, i) => row.classList.toggle("active", i === active));
    rows[active].scrollIntoView({ block: "nearest" });
  }

  document.querySelectorAll(selector).forEach((inp) => {
    inp.addEventListener("input", () => render(inp));
    inp.addEventListener("focus", () => render(inp, true));
    inp.addEventListener("blur", () => setTimeout(close, 150));
    inp.addEventListener("keydown", (ev) => {
      if (panel.hidden) return;
      if (ev.key === "ArrowDown") { ev.preventDefault(); highlight(1); }
      else if (ev.key === "ArrowUp") { ev.preventDefault(); highlight(-1); }
      else if (ev.key === "Enter" && (active >= 0 ||
               !projects.some((p) => p.c === inp.value.trim()))) {
        // Enter with nothing highlighted takes the top match, so a partial
        // code ("7100") never gets saved as-is.
        ev.preventDefault();
        const row = panel.children[Math.max(active, 0)];
        if (row) pick(row.querySelector("b").textContent);
      } else if (ev.key === "Escape") { close(); }
    });
  });
}
codeAutocomplete("projects-data", "input.project-input");
codeAutocomplete("accounts-data", "input.gl-input");

// The account title under each GL field follows the code as it changes.
(() => {
  const el = document.getElementById("accounts-data");
  if (!el) return;
  let titles = {};
  try { (JSON.parse(el.textContent) || []).forEach((a) => { titles[a.c] = a.n; }); }
  catch (e) { return; }
  document.querySelectorAll('input.gl-input[name^="gl_account_"]').forEach((inp) => {
    const hint = inp.parentElement.querySelector(".gl-title");
    if (!hint) return;
    const sync = () => { hint.textContent = titles[inp.value.trim()] || ""; };
    inp.addEventListener("input", sync);
    inp.addEventListener("change", sync);
  });
})();

// Hotel stays: one header per stay. Expand/collapse its component lines;
// the header checkbox ticks every line; header GL/Dept/Project inputs write
// straight into every line's own (submitted) fields — one decision per stay.
(() => {
  const children = (g) => document.querySelectorAll(`tr.grp-child[data-group="${g}"]`);
  document.querySelectorAll(".grp-toggle").forEach((btn) => {
    btn.addEventListener("click", () => {
      const open = btn.getAttribute("aria-expanded") !== "true";
      btn.setAttribute("aria-expanded", String(open));
      btn.textContent = btn.textContent.replace(/^[▸▾]/, open ? "▾" : "▸");
      children(btn.dataset.group).forEach((tr) => tr.classList.toggle("collapsed", !open));
    });
  });
  document.querySelectorAll(".grp-check").forEach((box) => {
    box.addEventListener("change", () => {
      children(box.dataset.group).forEach((tr) => {
        const b = tr.querySelector('input[type="checkbox"][name^="post_"]');
        if (b) b.checked = box.checked;
      });
    });
  });
  const apply = (el) => {
    children(el.dataset.group).forEach((tr) => {
      const target = tr.querySelector(`[name^="${el.dataset.field}_"]`);
      if (target) {
        target.value = el.value;
        target.dispatchEvent(new Event("input"));
      }
    });
  };
  document.querySelectorAll(".grp-apply").forEach((el) => {
    el.addEventListener("change", () => apply(el));
    if (el.tagName === "INPUT") el.addEventListener("input", () => apply(el));
  });
})();

// Bills (bank-drafted sources): one checkbox selects every line of the bill,
// stay checkboxes included — a bill posts whole, as one entry.
document.querySelectorAll(".bill-check").forEach((box) => {
  box.addEventListener("change", () => {
    document.querySelectorAll(`tr[data-bill="${box.dataset.bill}"]`).forEach((tr) => {
      tr.querySelectorAll('input[type="checkbox"]').forEach((b) => { b.checked = box.checked; });
    });
  });
});

// Live selection summary in the action bar: how many lines and how much
// money the next Approve would post.
(() => {
  const count = document.getElementById("sel-count");
  const total = document.getElementById("sel-total");
  if (!count || !total) return;
  const fmt = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" });
  const update = () => {
    let n = 0, sum = 0;
    document.querySelectorAll('input[type="checkbox"][name^="post_"]').forEach((b) => {
      if (!b.checked) return;
      n += 1;
      const tr = b.closest("tr");
      sum += parseFloat((tr && tr.dataset.amount) || "0") || 0;
    });
    count.textContent = String(n);
    total.textContent = fmt.format(sum);
  };
  document.addEventListener("change", update);
  document.addEventListener("click", () => setTimeout(update, 0));
  update();
})();
