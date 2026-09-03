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
    const box = tr.querySelector('input[type="checkbox"][name^="needs_review_"]');
    if (box) box.checked = on;
  });
}
const selAll = document.getElementById("select-visible");
const clrAll = document.getElementById("clear-visible");
if (selAll) selAll.addEventListener("click", () => setVisibleChecks(true));
if (clrAll) clrAll.addEventListener("click", () => setVisibleChecks(false));
