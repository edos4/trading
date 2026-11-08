// DeepSeek tab: probe the proxied DeepSeek Harness (dsh) UI and report its state.
//
// The probe is a document GET of /deepseek/, which is also what signs the
// browser in: the harness answers 401 until it has its session cookie, and the
// proxy replays the launch token to mint it. So a probe is never wasted work —
// by the time the frame loads, the cookie exists.
(function () {
  const status = document.getElementById("deepseek-status");
  const frame = document.getElementById("deepseek-frame");
  const reload = document.getElementById("deepseek-reload");
  if (!status || !frame) return;

  let signedOutReloaded = false;

  function set(text, ok) {
    status.textContent = text;
    status.style.color = ok ? "" : "var(--danger)";
  }

  async function check() {
    let resp;
    try {
      resp = await fetch("/deepseek/", {
        headers: { Accept: "text/html" },
        cache: "no-store",
        credentials: "same-origin",
      });
    } catch (err) {
      set("DeepSeek harness unreachable — is `dsh web` running on this host?", false);
      return;
    }
    if (resp.ok) {
      set("Connected — DeepSeek Harness is serving this page.", true);
      return;
    }
    if (resp.status === 401) {
      // No session cookie yet. The proxy already replayed the launch token on
      // this very request, so one frame reload picks the cookie up.
      if (!signedOutReloaded) {
        signedOutReloaded = true;
        set("Signing in to the DeepSeek harness…", true);
        setTimeout(reloadFrame, 400);
        return;
      }
      set("Not signed in. Set DEEPSEEK_HARNESS_TOKEN_FILE so the tab can sign "
        + "in automatically, or open the tokenized URL dsh printed at startup.", false);
      return;
    }
    if (resp.status === 404) {
      set("DeepSeek harness is not configured (DEEPSEEK_HARNESS_URL is empty).", false);
      return;
    }
    if (resp.status === 503) {
      set("DeepSeek harness unreachable — check DEEPSEEK_HARNESS_URL and that "
        + "deepseek-harness.service is running.", false);
      return;
    }
    set(`DeepSeek harness returned HTTP ${resp.status}.`, false);
  }

  function reloadFrame() {
    try {
      frame.contentWindow.location.reload();
    } catch (err) {
      frame.src = "/deepseek/";
    }
    check();
  }

  if (reload) reload.addEventListener("click", function () {
    signedOutReloaded = false;
    reloadFrame();
  });
  setInterval(check, 30000);
  check();
})();
