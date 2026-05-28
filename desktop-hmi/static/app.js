// HVAC HMI — frontend
//
// Connects to the FastAPI WebSocket at /ws, renders the latest status
// snapshot, and (for inputs) lets you override the live hardware reading
// to simulate thermostat calls.  Override commands are sent back over the
// same WebSocket.

(() => {
  const $ = (id) => document.getElementById(id);
  const led = $("led");
  const connText = $("connection-text");
  const hostEl = $("host");

  const STALE_SECONDS = 30;   // sensor data older than this is flagged

  let metadata = null;        // {outputs, inputs, controller_host}
  let lastSnap = null;
  let ws = null;
  let wsReconnectDelay = 1000;

  // ────────────────────────────────────────────────
  // Render helpers
  // ────────────────────────────────────────────────

  function buildOutputCard(f) {
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
    return card;
  }

  function buildInputCard(f) {
    const card = document.createElement("div");
    card.className = "iocard input-card unknown";
    card.dataset.key = f.key;
    card.innerHTML = `
      <div class="iocard-row1">
        <div class="iocard-label">${escapeHtml(f.label)}<span class="forced-badge">FORCED</span></div>
        <div class="iocard-state">···</div>
      </div>
      <div class="iocard-desc">${escapeHtml(f.desc)}</div>
      <div class="override-controls">
        <button class="ovr-btn" data-mode="auto">AUTO</button>
        <button class="ovr-btn" data-mode="on">FORCE ON</button>
        <button class="ovr-btn" data-mode="off">FORCE OFF</button>
      </div>
    `;
    // Wire override buttons
    card.querySelectorAll(".ovr-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        sendOverride(f.key, btn.dataset.mode);
      });
    });
    return card;
  }

  function buildGrids() {
    const inG = $("inputs-grid");
    const outG = $("outputs-grid");
    inG.innerHTML = "";
    outG.innerHTML = "";
    metadata.inputs.forEach((f) => inG.appendChild(buildInputCard(f)));
    metadata.outputs.forEach((f) => outG.appendChild(buildOutputCard(f)));
  }

  function updateOutputs(valueMap) {
    if (!valueMap) return;
    document.querySelectorAll("#outputs-grid .iocard").forEach((card) => {
      const v = valueMap[card.dataset.key];
      const stateEl = card.querySelector(".iocard-state");
      card.classList.remove("on", "unknown", "bad");
      if (v === true) { card.classList.add("on"); stateEl.textContent = "ON"; }
      else if (v === false) { stateEl.textContent = "OFF"; }
      else { card.classList.add("unknown"); stateEl.textContent = "···"; }
    });
  }

  function updateInputs(valueMap, overrideMap) {
    document.querySelectorAll("#inputs-grid .iocard").forEach((card) => {
      const key = card.dataset.key;
      const v = valueMap ? valueMap[key] : undefined;
      const mode = (overrideMap && overrideMap[key]) || "auto";
      const stateEl = card.querySelector(".iocard-state");

      card.classList.remove("on", "unknown", "bad", "forced");
      if (v === true) { card.classList.add("on"); stateEl.textContent = "ON"; }
      else if (v === false) { stateEl.textContent = "OFF"; }
      else { card.classList.add("unknown"); stateEl.textContent = "···"; }

      if (mode !== "auto") card.classList.add("forced");

      // Highlight the active override button
      card.querySelectorAll(".ovr-btn").forEach((btn) => {
        btn.classList.toggle("active", btn.dataset.mode === mode);
      });
    });
  }

  function updateSimBanner(snap) {
    const banner = $("sim-banner");
    const ovr = snap.input_override || {};
    const forced = Object.entries(ovr).filter(([, m]) => m !== "auto");
    if (forced.length > 0) {
      banner.hidden = false;
      const names = forced.map(([k]) => {
        const meta = metadata.inputs.find((f) => f.key === k);
        return meta ? meta.label : k;
      });
      $("sim-banner-text").textContent =
        `Simulation mode active — ${forced.length} input(s) forced: ${names.join(", ")}. ` +
        `The MCU is driving real equipment from these simulated calls.`;
    } else {
      banner.hidden = true;
    }
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
    if (snap && snap.controller_host) hostEl.textContent = snap.controller_host;
  }

  function fmt(v, digits = 1) {
    if (v === null || v === undefined || Number.isNaN(v)) return "—";
    return Number(v).toFixed(digits);
  }

  function updateHero(snap) {
    // Mode
    const modeEl = $("mode");
    const mode = (snap.mode || "—").toString();
    modeEl.textContent = mode.toUpperCase().replace(/_/g, " ");
    modeEl.className = "hero-value";
    if (mode === "off") modeEl.classList.add("is-off");
    else if (mode === "fan") modeEl.classList.add("is-fan");
    else if (mode.includes("cool")) modeEl.classList.add("is-cool");
    else if (mode.includes("heat")) modeEl.classList.add("is-heat");
    else if (mode.includes("dehum")) modeEl.classList.add("is-dehum");

    // Compressor
    const compEl = $("compressor");
    if (snap.compressor_on === true) { compEl.textContent = "RUNNING"; compEl.className = "hero-value is-on"; }
    else if (snap.compressor_on === false) { compEl.textContent = "STOPPED"; compEl.className = "hero-value is-off"; }
    else { compEl.textContent = "—"; compEl.className = "hero-value"; }

    // Sensors
    const s = snap.sensors || {};
    $("indoor-temp").textContent  = fmt(s.indoor_temp_f);
    $("indoor-hum").textContent   = s.indoor_humidity_pct != null ? `${fmt(s.indoor_humidity_pct, 0)} %RH` : "— %RH";
    $("outdoor-temp").textContent = fmt(s.outdoor_temp_f);
    $("outdoor-hum").textContent  = s.outdoor_humidity_pct != null ? `${fmt(s.outdoor_humidity_pct, 0)} %RH` : "— %RH";
    $("ac-power").textContent      = s.ac_power_w   != null ? fmt(s.ac_power_w, 0) : "—";
    $("ac-current").textContent    = s.ac_current_a != null ? fmt(s.ac_current_a, 1) : "—";
    $("ac-voltage").textContent    = s.ac_voltage_v != null ? fmt(s.ac_voltage_v, 0) : "—";

    // Staleness: compare the MQTT message timestamp to the server poll time.
    // Using both server-side values avoids client-clock skew.
    let stale = true;
    if (snap.sensors && snap.mqtt_ts && snap.ts) {
      stale = (snap.ts - snap.mqtt_ts) > STALE_SECONDS;
    }
    ["card-indoor", "card-outdoor", "card-power"].forEach((id) => {
      $(id).classList.toggle("stale", stale);
    });
  }

  function applySnapshot(snap) {
    lastSnap = snap;
    if (!metadata) return;

    setConnection(snap.connected ? "connected" : "controller-down", snap);
    updateHero(snap);
    updateInputs(snap.inputs, snap.input_override);
    updateOutputs(snap.outputs);
    updateSimBanner(snap);

    const ts = snap.ts ? new Date(snap.ts * 1000) : new Date();
    let footer = `Last update: ${ts.toLocaleTimeString()}`;
    if (snap.sim_active) footer += "  •  SIMULATION ACTIVE";
    $("last-update").textContent = footer;
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;"
    }[c]));
  }

  // ────────────────────────────────────────────────
  // Override commands (WebSocket → MCU)
  // ────────────────────────────────────────────────

  function sendWS(obj) {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(obj));
      return true;
    }
    return false;
  }

  function sendOverride(key, mode) {
    sendWS({ type: "override", key, mode });
  }

  function clearAllOverrides() {
    sendWS({ type: "clear_overrides" });
  }

  // ────────────────────────────────────────────────
  // Bootstrap
  // ────────────────────────────────────────────────

  async function fetchMetadata() {
    try {
      const resp = await fetch("/api/metadata");
      metadata = await resp.json();
      buildGrids();
      if (metadata.controller_host) hostEl.textContent = metadata.controller_host;
      if (lastSnap) applySnapshot(lastSnap);
    } catch (e) {
      console.error("metadata fetch failed", e);
      setConnection("metadata failed");
    }
  }

  function connectWS() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    ws = new WebSocket(`${proto}//${location.host}/ws`);

    ws.addEventListener("open", () => { wsReconnectDelay = 1000; });
    ws.addEventListener("message", (ev) => {
      let data;
      try { data = JSON.parse(ev.data); } catch { return; }
      if (data.type === "ack") return;     // override command acknowledgement
      applySnapshot(data);
    });
    ws.addEventListener("close", () => {
      setConnection("server disconnected");
      setTimeout(connectWS, wsReconnectDelay);
      wsReconnectDelay = Math.min(wsReconnectDelay * 2, 10000);
    });
  }

  document.addEventListener("DOMContentLoaded", async () => {
    $("sim-clear-btn").addEventListener("click", clearAllOverrides);
    await fetchMetadata();
    connectWS();
  });
})();
