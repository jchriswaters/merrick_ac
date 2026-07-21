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

  // ────────────────────────────────────────────────
  // Settings panel
  // ────────────────────────────────────────────────

  function buildSettingsPanel() {
    const grid = $("settings-grid");
    grid.innerHTML = "";
    (metadata.config_fields || []).forEach((f) => {
      const row = document.createElement("div");
      row.className = "setting-row";
      row.dataset.key = f.key;
      row.dataset.kind = f.kind;
      if (f.min != null) row.dataset.min = f.min;
      if (f.max != null) row.dataset.max = f.max;

      const labelDiv = document.createElement("div");
      labelDiv.className = "setting-label";
      labelDiv.innerHTML = `
        <div class="setting-label-main">${escapeHtml(f.label)}</div>
        <div class="setting-help">${escapeHtml(f.help || "")}</div>
      `;

      const controlDiv = document.createElement("div");
      controlDiv.className = "setting-control";

      let inputEl;
      if (f.kind === "bool") {
        controlDiv.innerHTML = `
          <label class="setting-toggle">
            <input type="checkbox">
            <span class="slider"></span>
          </label>
        `;
        inputEl = controlDiv.querySelector("input[type=checkbox]");
      } else if (f.kind === "enum") {
        const sel = document.createElement("select");
        (f.choices || []).forEach((opt) => {
          const o = document.createElement("option");
          o.value = opt; o.textContent = opt;
          sel.appendChild(o);
        });
        controlDiv.appendChild(sel);
        inputEl = sel;
      } else {  // int / number
        const num = document.createElement("input");
        num.type = "number";
        if (f.min != null) num.min = f.min;
        if (f.max != null) num.max = f.max;
        num.step = 1;
        num.readOnly = true;
        num.style.cursor = "pointer";
        num.addEventListener("click", () => openNumpad(num, f));
        controlDiv.appendChild(num);
        if (f.unit) {
          const u = document.createElement("span");
          u.className = "setting-unit"; u.textContent = f.unit;
          controlDiv.appendChild(u);
        }
        inputEl = num;
      }
      inputEl.dataset.role = "value";
      inputEl.addEventListener("input",  () => markDirty(row));
      inputEl.addEventListener("change", () => markDirty(row));

      const saveBtn = document.createElement("button");
      saveBtn.className = "setting-save";
      saveBtn.textContent = "Save";
      saveBtn.disabled = true;
      saveBtn.addEventListener("click", () => saveSetting(row));

      row.appendChild(labelDiv);
      row.appendChild(controlDiv);
      row.appendChild(saveBtn);
      grid.appendChild(row);
    });
  }

  function readControlValue(row) {
    const ctrl = row.querySelector("[data-role=value]");
    const kind = row.dataset.kind;
    if (kind === "bool") return ctrl.checked;
    if (kind === "enum") return ctrl.value;
    const n = parseFloat(ctrl.value);
    return Number.isFinite(n) ? n : null;
  }

  function setControlValue(row, value) {
    const ctrl = row.querySelector("[data-role=value]");
    const kind = row.dataset.kind;
    if (kind === "bool") ctrl.checked = !!value;
    else ctrl.value = (value === null || value === undefined) ? "" : value;
    row.dataset.serverValue = JSON.stringify(value);
    markClean(row);
  }

  function markDirty(row) {
    const current = readControlValue(row);
    const serverStr = row.dataset.serverValue || "null";
    const isDirty = JSON.stringify(current) !== serverStr;
    row.classList.toggle("dirty", isDirty);
    row.querySelector(".setting-save").disabled = !isDirty;
  }
  function markClean(row) {
    row.classList.remove("dirty");
    row.querySelector(".setting-save").disabled = true;
  }

  async function saveSetting(row) {
    const key = row.dataset.key;
    const value = readControlValue(row);
    if (value === null) return;
    const btn = row.querySelector(".setting-save");
    btn.disabled = true; btn.textContent = "Saving…";
    try {
      const ok = sendWS({ type: "set_config", key, value });
      if (!ok) {
        // WebSocket not open — fall back to REST
        const resp = await fetch("/api/set_config", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ type: "set_config", key, value }),
        });
        await resp.json();
      }
    } catch (e) { console.error("save failed", e); }
    btn.textContent = "Save";
    // Mark clean immediately so updateSettings() doesn't skip this row on
    // the next snapshot (it skips dirty rows to avoid clobbering in-flight edits).
    row.dataset.serverValue = JSON.stringify(value);
    markClean(row);
  }

  function updateSettings(snap) {
    const cfg = snap.config;
    if (!cfg) return;
    document.querySelectorAll("#settings-grid .setting-row").forEach((row) => {
      // Don't clobber a value the user is currently editing.
      if (row.classList.contains("dirty")) return;
      const key = row.dataset.key;
      if (key in cfg) setControlValue(row, cfg[key]);
    });
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
    led.classList.remove("on", "bad", "warn");
    if (state === "connected") {
      // Connected — but is the MCU healthy?  Bridge publishes mcu_healthy
      // every cycle; if it's flipped to false the controller is up but
      // the MCU has gone silent (likely needs a router restart / SRST).
      if (snap && snap.mcu_healthy === false) {
        led.classList.add("warn");
        const sil = snap.mcu_silence_s;
        connText.textContent = sil != null
          ? `MCU unresponsive (${Math.round(sil)} s)`
          : "MCU unresponsive";
      } else {
        led.classList.add("on");
        connText.textContent = "Connected";
      }
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

    // Heat-pump compressor — derived in bridge_daemon from the low_cool
    // / high_cool relays, so it's accurate even without the SDM120 wired.
    const compEl = $("compressor");
    const compSub = $("compressor-sub");
    const outs = snap.outputs || {};
    let stageLabel = "stage idle";
    if (outs.high_cool) stageLabel = outs.reversing_valve ? "high heat (heat pump)" : "high cool";
    else if (outs.low_cool) stageLabel = outs.reversing_valve ? "low heat (heat pump)" : "low cool";
    if (snap.compressor_on === true) { compEl.textContent = "RUNNING"; compEl.className = "hero-value is-on"; }
    else if (snap.compressor_on === false) { compEl.textContent = "STOPPED"; compEl.className = "hero-value is-off"; }
    else { compEl.textContent = "—"; compEl.className = "hero-value"; }
    if (compSub) compSub.textContent = stageLabel;

    // Sensors  (temp and humidity get equal visual weight now)
    const s = snap.sensors || {};
    $("indoor-temp").textContent  = fmt(s.indoor_temp_f);
    $("indoor-hum").textContent   = s.indoor_humidity_pct != null ? fmt(s.indoor_humidity_pct, 0) : "—";
    $("outdoor-temp").textContent = fmt(s.outdoor_temp_f);
    $("outdoor-hum").textContent  = s.outdoor_humidity_pct != null ? fmt(s.outdoor_humidity_pct, 0) : "—";
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
    updateSettings(snap);

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
  // Numeric keypad modal
  // ────────────────────────────────────────────────

  let numpadTarget = null;   // the <input> we're editing
  let numpadField  = null;   // field metadata {label, min, max, unit}
  let numpadVal    = "";     // string being built

  function openNumpad(inputEl, field) {
    numpadTarget = inputEl;
    numpadField  = field;
    numpadVal    = inputEl.value !== "" ? String(inputEl.value) : "";

    $("numpad-title").textContent = field.label;
    const hasRange = field.min != null || field.max != null;
    $("numpad-range").textContent = hasRange
      ? `Range: ${field.min ?? "—"} – ${field.max ?? "—"}${field.unit ? " " + field.unit : ""}`
      : "";
    renderNumpadDisplay();
    $("numpad-overlay").hidden = false;
  }

  function renderNumpadDisplay() {
    const disp = $("numpad-display");
    disp.textContent = numpadVal === "" ? "0" : numpadVal;
    const n = parseFloat(numpadVal);
    const outOfRange = numpadVal !== "" && Number.isFinite(n) && (
      (numpadField.min != null && n < numpadField.min) ||
      (numpadField.max != null && n > numpadField.max)
    );
    disp.classList.toggle("invalid", outOfRange);
    $("numpad-ok").disabled = numpadVal === "" || !Number.isFinite(n) || outOfRange;
  }

  function numpadPress(val) {
    if (val === "bksp") {
      numpadVal = numpadVal.slice(0, -1);
    } else {
      if (numpadVal === "0") numpadVal = "";
      numpadVal += val;
    }
    renderNumpadDisplay();
  }

  function numpadCommit() {
    const n = parseFloat(numpadVal);
    if (!Number.isFinite(n)) return;
    numpadTarget.value = n;
    numpadTarget.dispatchEvent(new Event("change", { bubbles: true }));
    closeNumpad();
  }

  function closeNumpad() {
    $("numpad-overlay").hidden = true;
    numpadTarget = null;
  }

  // ────────────────────────────────────────────────
  // Bootstrap
  // ────────────────────────────────────────────────

  async function fetchMetadata() {
    try {
      const resp = await fetch("/api/metadata");
      metadata = await resp.json();
      buildGrids();
      buildSettingsPanel();
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

    // Numpad key events
    document.querySelectorAll(".nk").forEach((btn) => {
      btn.addEventListener("click", () => {
        const action = btn.dataset.action;
        numpadPress(action || btn.dataset.val);
      });
    });
    $("numpad-ok").addEventListener("click", numpadCommit);
    $("numpad-cancel").addEventListener("click", closeNumpad);
    $("numpad-overlay").addEventListener("click", (e) => {
      if (e.target === $("numpad-overlay")) closeNumpad();
    });

    await fetchMetadata();
    connectWS();
  });
})();
