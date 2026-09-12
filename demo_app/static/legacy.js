document.querySelectorAll('form[data-operations-dialog="true"]').forEach((form) => {
  form.addEventListener("submit", (event) => {
    const accepted = window.confirm(
      "Operations notice: end-of-day posting is active. Confirm that a supervisor approved this transfer."
    );
    if (!accepted) event.preventDefault();
  });
});
