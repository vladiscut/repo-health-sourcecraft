(function () {
  var root = document.querySelector("[data-scan-status-url]");
  if (!root) {
    return;
  }

  var url = root.getAttribute("data-scan-status-url");
  if (!url) {
    return;
  }

  var phrases = {
    checking: root.getAttribute("data-phrase-checking"),
    pending: root.getAttribute("data-phrase-pending"),
    running: root.getAttribute("data-phrase-running"),
  };
  var intervalMs = 2000;

  function show(status) {
    var text = phrases[status];
    if (!text) {
      window.location.reload();
      return;
    }
    var nodes = document.querySelectorAll("[data-scan-phase-text]");
    for (var i = 0; i < nodes.length; i++) {
      nodes[i].textContent = text;
    }
  }

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
        show(data && data.status);
      })
      .catch(function () {
        /* сеть временно недоступна — следующий тик */
      });
  }

  window.setInterval(poll, intervalMs);
})();
