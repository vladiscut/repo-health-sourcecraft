(function () {
  var root = document.querySelector("[data-scan-status-url]");
  if (!root) {
    return;
  }

  var url = root.getAttribute("data-scan-status-url");
  if (!url) {
    return;
  }

  var intervalMs = 2000;

  function poll() {
    fetch(url, {
      headers: { Accept: "application/json" },
      credentials: "same-origin",
    })
      .then(function (response) {
        if (!response.ok) {
          throw new Error("status " + response.status);
        }
        return response.json();
      })
      .then(function (data) {
        var status = data && data.status;
        if (status === "pending" || status === "running") {
          return;
        }
        window.location.reload();
      })
      .catch(function () {
        /* сеть временно недоступна — следующий тик */
      });
  }

  window.setInterval(poll, intervalMs);
})();
