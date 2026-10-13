// Hermes tab: probe the proxied Hermes dashboard and report its state.
(function () {
  const status = document.getElementById("hermes-status");
  const frame = document.getElementById("hermes-frame");
  const reload = document.getElementById("hermes-reload");
  if (!status || !frame) return;

  function set(text, ok) {
    status.textContent = text;
    status.style.color = ok ? "" : "var(--danger)";
  }

  async function check() {
    try {
      const resp = await fetch("/hermes/api/status", { headers: { Accept: "application/json" } });
      if (!resp.ok) {
        set(`Hermes dashboard returned HTTP ${resp.status}.`, false);
        return;
      }
      const data = await resp.json();
      const gateway = data.gateway_state ? `gateway ${data.gateway_state}` : "gateway unknown";
      set(`Connected — Hermes ${data.version || "?"} · ${gateway}.`, true);
    } catch (err) {
      set("Hermes dashboard unreachable — is `hermes dashboard` running on this host?", false);
    }
  }

  function reloadFrame() {
    try {
      frame.contentWindow.location.reload();
    } catch (err) {
      frame.src = "/hermes/";
    }
    check();
  }

  if (reload) reload.addEventListener("click", reloadFrame);
  frame.addEventListener("load", check);
  setInterval(check, 30000);
  check();
})();
