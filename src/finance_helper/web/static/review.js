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
