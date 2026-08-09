(function () {
  const dialog = document.querySelector("[data-subscribe-dialog]");
  const form = document.querySelector("[data-subscribe-form]");
  const errorBox = document.querySelector("[data-subscribe-error]");
  const toast = document.querySelector("[data-subscribe-toast]");
  const openButtons = document.querySelectorAll("[data-subscribe-open]");
  const closeButtons = document.querySelectorAll("[data-subscribe-close]");

  if (!dialog || !form || !errorBox || !toast || openButtons.length === 0) {
    return;
  }

  let lastFocusedElement = null;

  function showElement(element) {
    element.removeAttribute("hidden");
  }

  function hideElement(element) {
    element.setAttribute("hidden", "");
  }

  function openDialog() {
    lastFocusedElement = document.activeElement;
    hideElement(errorBox);
    errorBox.textContent = "";
    showElement(dialog);
    const firstInput = form.querySelector("input[name='name']");
    if (firstInput) {
      firstInput.focus();
    }
  }

  function closeDialog() {
    hideElement(dialog);
    form.reset();
    hideElement(errorBox);
    errorBox.textContent = "";
    if (lastFocusedElement) {
      lastFocusedElement.focus();
    }
  }

  function showToast(message) {
    toast.textContent = message;
    showElement(toast);
    window.setTimeout(function () {
      hideElement(toast);
    }, 5000);
  }

  function getCsrfToken() {
    const tokenInput = form.querySelector("input[name='csrfmiddlewaretoken']");
    return tokenInput ? tokenInput.value : "";
  }

  function trimFields() {
    form.querySelectorAll("input").forEach(function (input) {
      input.value = input.value.trim();
    });
  }

  openButtons.forEach(function (button) {
    button.addEventListener("click", function () {
      openDialog();
    });
  });

  closeButtons.forEach(function (button) {
    button.addEventListener("click", function () {
      closeDialog();
    });
  });

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && !dialog.hasAttribute("hidden")) {
      closeDialog();
    }
  });

  form.addEventListener("submit", function (event) {
    event.preventDefault();
    trimFields();

    if (!form.checkValidity()) {
      form.reportValidity();
      return;
    }

    const submitButton = form.querySelector("button[type='submit']");
    if (submitButton) {
      submitButton.disabled = true;
      submitButton.textContent = "Subscribing...";
    }

    fetch(form.action, {
      method: "POST",
      headers: {
        "X-CSRFToken": getCsrfToken(),
        "X-Requested-With": "XMLHttpRequest",
      },
      body: new FormData(form),
      credentials: "same-origin",
    })
      .then(function (response) {
        return response.json().then(function (data) {
          if (!response.ok) {
            throw data;
          }
          return data;
        });
      })
      .then(function (data) {
        closeDialog();
        showToast("🎉 " + data.message);
      })
      .catch(function (data) {
        errorBox.textContent = data && data.message ? data.message : "Please check your details and try again.";
        showElement(errorBox);
      })
      .finally(function () {
        if (submitButton) {
          submitButton.disabled = false;
          submitButton.textContent = "Subscribe";
        }
      });
  });
})();
