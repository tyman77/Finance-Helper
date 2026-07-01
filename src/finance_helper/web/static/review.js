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
