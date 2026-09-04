// Filter toolbar: show/hide rows by their data-status, purely client-side.
document.querySelectorAll(".filter-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".filter-btn").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    const filter = btn.dataset.filter;
    document.querySelectorAll("tbody tr").forEach((tr) => {
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

// Project autocomplete: the native <datalist> popup truncates long project
// names and can't be styled, so the project fields get a custom dropdown —
// full names shown, searchable by code OR name ("amarillo" finds 2339).
(() => {
  const el = document.getElementById("projects-data");
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

  function render(input) {
    const q = input.value.trim().toLowerCase();
    const hits = projects.filter((p) =>
      !q || p.c.startsWith(q) || p.n.toLowerCase().includes(q)).slice(0, 12);
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

  document.querySelectorAll("input.project-input").forEach((inp) => {
    inp.addEventListener("input", () => render(inp));
    inp.addEventListener("focus", () => render(inp));
    inp.addEventListener("blur", () => setTimeout(close, 150));
    inp.addEventListener("keydown", (ev) => {
      if (panel.hidden) return;
      if (ev.key === "ArrowDown") { ev.preventDefault(); highlight(1); }
      else if (ev.key === "ArrowUp") { ev.preventDefault(); highlight(-1); }
      else if (ev.key === "Enter" && active >= 0) {
        ev.preventDefault();
        const row = panel.children[active];
        if (row) pick(row.querySelector("b").textContent);
      } else if (ev.key === "Escape") { close(); }
    });
  });
})();
