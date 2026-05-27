// HVAC HMI — frontend
//
// Connects to the FastAPI WebSocket at /ws and renders the latest status
// snapshot.  Phase 1 is read-only; the framework here is set up so that
// Phase 2 (input simulation) only needs a click handler that sends a
// JSON command back over the same socket.

(() => {
  const $ = (id) => document.getElementById(id);
  const led = $("led");
  const connText = $("connection-text");
  const hostEl = $("host");

  let metadata = null;     // {outputs: [...], inputs: [...], controller_host}
  let lastSnap = null;
  let ws = null;
  let wsReconnectDelay = 1000;

  // ────────────────────────────────────────────────
  // Render helpers
  // ────────────────────────────────────────────────

  function buildGrid(containerId, fields, valueMap) {
    const container = $(containerId);
    container.innerHTML = "";
    fields.forEach((f) => {
      const card = document.createElement("div");
      card.className = "iocard unknown";
      card.dataset.key = f.key;
      card.innerHTML = `
        <div class="iocard-row1">
          <div class="iocard-label">${escapeHtml(f.label)}</div>
          <div class="iocard-state">···</div>
        </div>
        <div class="iocard-desc">${escapeHtml(f.desc)}</div>
      `;
      container.appendChild(card);
    });
  }

  function updateGrid(containerId, valueMap) {
    if (!valueMap) return;
    const cards = document.querySelectorAll(`#${containerId} .iocard`);
    cards.forEach((card) => {
      const key = card.dataset.key;
      const v = valueMap[key];
      card.classList.remove("on", "unknown", "bad");
      const stateEl = card.querySelector(".iocard-state");
      if (v === true) {
        card.classList.add("on");
        stateEl.textContent = "ON";
      } else if (v === false) {
        stateEl.textContent = "OFF";
      } else {
        card.classList.add("unknown");
        stateEl.textContent = "···";
      }
    });
  }

  function setConnection(state, snap) {
    led.classList.remove("on", "bad");
    if (state === "connected") {
      led.classList.add("on");
      connText.textContent = "Connected";
    } else if (state === "controller-down") {
      led.classList.add("bad");
      connText.textContent = "Controller unreachable";
    } else {
      connText.textContent = state;
    }
    if (snap && snap.controller_host) {
      hostEl.textContent = snap.controller_host;
    }
  }

  function formatNumber(v, digits = 1) {
    if (v === null || v === undefined || Number.isNaN(v)) return "—";
    return Number(v).toFixed(digits);
  }

  function updateHero(snap) {
    // Mode
    const modeEl = $("mode");
    const mode = (snap.mode || "—").toString();
    modeEl.textContent = mode.toUpperCase().replace(/_/g, " ");
    modeEl.className = "hero-value";
    if (mode === "off")            modeEl.classList.add("is-off");
    else if (mode === "fan")       modeEl.classList.add("is-fan");
    else if (mode.includes("cool"))modeEl.classList.add("is-cool");
    else if (mode.includes("heat"))modeEl.classList.add("is-heat");
    else if (mode.includes("dehum"))modeEl.classList.add("is-dehum");

    // Compressor
    const compEl = $("compressor");
    if (snap.compressor_on === true)  { compEl.textContent = "RUNNING"; compEl.className = "hero-value is-on"; }
    else if (snap.compressor_on === false) { compEl.textContent = "STOPPED"; compEl.className = "hero-value is-off"; }
    else                              { compEl.textContent = "—";       compEl.className = "hero-value"; }

    // Sensors
    const s = snap.sensors || {};
    $("indoor-temp").textContent  = formatNumber(s.indoor_temp_f);
    $("indoor-hum").textContent   = s.indoor_humidity_pct != null
        ? `${formatNumber(s.indoor_humidity_pct, 0)} %RH` : "— %RH";
    $("outdoor-temp").textContent = formatNumber(s.outdoor_temp_f);
    $("outdoor-hum").textContent  = s.outdoor_humidity_pct != null
        ? `${formatNumber(s.outdoor_humidity_pct, 0)} %RH` : "— %RH";

    $("ac-power").textContent     = s.ac_power_w   != null ? formatNumber(s.ac_power_w, 0) : "—";
    $("ac-current").textContent   = s.ac_current_a != null ? formatNumber(s.ac_current_a, 1) : "—";
    $("ac-voltage").textContent   = s.ac_voltage_v != null ? formatNumber(s.ac_voltage_v, 0) : "—";
  }

  function applySnapshot(snap) {
    lastSnap = snap;
    if (!metadata) return;       // hold off until grids exist

    if (snap.connected) {
      setConnection("connected", snap);
    } else {
      setConnection("controller-down", snap);
    }

    updateHero(snap);
    updateGrid("inputs-grid",  snap.inputs);
    updateGrid("outputs-grid", snap.outputs);

    // Last update timestamp
    const ts = snap.ts ? new Date(snap.ts * 1000) : new Date();
    $("last-update").textContent = `Last update: ${ts.toLocaleTimeString()}`;
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({
      "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#039;"
    }[c]));
  }

  // ────────────────────────────────────────────────
  // Bootstrap: fetch metadata, build grids, open WS
  // ────────────────────────────────────────────────

  async function fetchMetadata() {
    try {
      const resp = await fetch("/api/metadata");
      metadata = await resp.json();
      buildGrid("inputs-grid",  metadata.inputs);
      buildGrid("outputs-grid", metadata.outputs);
      if (metadata.controller_host) hostEl.textContent = metadata.controller_host;
      if (lastSnap) applySnapshot(lastSnap);
    } catch (e) {
      console.error("metadata fetch failed", e);
      setConnection("metadata failed");
    }
  }

  function connectWS() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const url = `${proto}//${location.host}/ws`;
    ws = new WebSocket(url);

    ws.addEventListener("open", () => {
      wsReconnectDelay = 1000;
      // setConnection updated when first snapshot arrives
    });

    ws.addEventListener("message", (ev) => {
      try {
        const snap = JSON.parse(ev.data);
        applySnapshot(snap);
      } catch (e) {
        console.error("bad ws message", e);
      }
    });

    ws.addEventListener("close", () => {
      setConnection("server disconnected");
      // backoff reconnect
      setTimeout(connectWS, wsReconnectDelay);
      wsReconnectDelay = Math.min(wsReconnectDelay * 2, 10000);
    });

    ws.addEventListener("error", () => {
      // close handler will fire next
    });
  }

  // ────────────────────────────────────────────────

  document.addEventListener("DOMContentLoaded", async () => {
    await fetchMetadata();
    connectWS();
  });
})();
