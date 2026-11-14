let appState = null;
let recordingHotkey = false;

const el = (id) => document.getElementById(id);

async function api(url, opts = {}) {
  const res = await fetch(url, opts);
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail || "Request failed");
  }
  return res.json();
}

async function refreshState() {
  appState = await api("/api/state");
  renderAll();
}

function renderAll() {
  if (!appState) return;
  const s = appState.settings;

  el("discord-hint").innerHTML = appState.auto_mic_switched
    ? `🟢 Windows Default Mic auto-routed to <strong>${appState.recommended_discord_mic}</strong> (restores on exit)`
    : `Select <strong id="discord-mic-name">${appState.recommended_discord_mic}</strong> in Discord / Game`;
  el("stop-kbd").textContent = (s.stop_all_hotkey || "F12").toUpperCase();

  const micBtn = el("btn-mic-mute");
  micBtn.textContent = s.mic_muted ? "🔇 Mic Muted" : "🎙️ Mic Live";
  micBtn.classList.toggle("muted", !!s.mic_muted);

  el("slider-mic-gain").value = s.mic_gain;
  el("mic-gain-val").textContent = `${Math.round(s.mic_gain * 100)}%`;

  el("slider-sb-gain").value = s.soundboard_gain;
  el("sb-gain-val").textContent = `${Math.round(s.soundboard_gain * 100)}%`;

  el("chk-monitor").checked = !!s.monitor_enabled;
  el("slider-mon-gain").value = s.monitor_gain;
  el("mon-gain-val").textContent = `${Math.round(s.monitor_gain * 100)}%`;

  el("chk-polyphonic").checked = !!s.polyphonic;
  el("chk-duck").checked = !!s.duck_mic;
  el("chk-duck-music").checked = !!s.duck_music_on_voice;
  el("chk-queue").checked = !!s.queue_mode;
  el("chk-auto-mic").checked = s.auto_switch_default_mic !== false;

  const db = appState.effective_vout_gain_db;
  el("slider-vout-db").value = db;
  el("vout-db-label").textContent = `${db.toFixed(1)} dB`;

  renderDeviceSelects();
  renderPads();
}

function renderDeviceSelects() {
  const devs = appState.devices || [];
  const s = appState.settings;

  const fill = (selectId, items, current) => {
    const sel = el(selectId);
    sel.innerHTML = "";
    const autoOpt = document.createElement("option");
    autoOpt.value = "auto";
    autoOpt.textContent = "Auto-Detect (Recommended)";
    sel.appendChild(autoOpt);
    for (const d of items) {
      const opt = document.createElement("option");
      opt.value = d.name;
      opt.textContent = d.name;
      sel.appendChild(opt);
    }
    sel.value = current || "auto";
  };

  fill("sel-mic", devs.filter((d) => d.max_inputs > 0), s.mic_device);
  fill("sel-vout", devs.filter((d) => d.max_outputs > 0), s.virtual_out_device);
  fill("sel-mon", devs.filter((d) => d.max_outputs > 0), s.monitor_out_device);
}

function renderPads() {
  const grid = el("pad-grid");
  const pads = appState.pads || [];
  el("empty-state").classList.toggle("hidden", pads.length > 0);
  grid.innerHTML = "";

  for (const pad of pads) {
    const card = document.createElement("div");
    card.className = "pad";
    card.dataset.padId = pad.id;
    card.style.setProperty("--pad-color", pad.color || "#5865f2");

    const top = document.createElement("div");
    top.className = "pad-top";

    const badge = document.createElement("span");
    badge.className = "pad-badge";
    const tags = [];
    if (pad.hotkey) tags.push(pad.hotkey.toUpperCase());
    if (pad.loop) tags.push("LOOP");
    badge.textContent = tags.join(" • ") || "CLIP";

    const editBtn = document.createElement("button");
    editBtn.className = "pad-edit";
    editBtn.textContent = "✎";
    editBtn.title = "Edit pad / hotkey";
    editBtn.onclick = (e) => {
      e.stopPropagation();
      openPadEditor(pad);
    };

    top.append(badge, editBtn);

    const name = document.createElement("div");
    name.className = "pad-name";
    name.textContent = pad.name;

    const prog = document.createElement("div");
    prog.className = "pad-progress";
    prog.id = `prog-${pad.id}`;

    card.append(top, name, prog);
    card.onclick = (e) => playOrStopPad(pad.id, e.shiftKey);
    grid.appendChild(card);
  }
}

async function playOrStopPad(padId, stack = false) {
  await api(`/api/pads/${padId}/play?stack=${stack ? "true" : "false"}`, { method: "POST" });
}

async function saveSetting(patch) {
  await api("/api/settings", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
}

async function uploadFiles(files) {
  for (const file of files) {
    const fd = new FormData();
    fd.append("file", file);
    try {
      await api("/api/pads/upload", { method: "POST", body: fd });
    } catch (err) {
      alert(`Could not add ${file.name}: ${err.message}`);
    }
  }
  await refreshState();
}

function openPadEditor(pad) {
  el("edit-pad-id").value = pad.id;
  el("edit-pad-name").value = pad.name;
  el("edit-pad-vol").value = pad.volume;
  el("edit-pad-vol-label").textContent = `${Math.round(pad.volume * 100)}%`;
  el("edit-pad-hotkey").value = pad.hotkey || "";
  el("edit-pad-start").value = pad.start_sec ?? 0;
  el("edit-pad-end").value = pad.end_sec ?? "";
  el("edit-pad-loop").checked = !!pad.loop;
  el("edit-pad-color").value = pad.color || "#5865f2";
  el("modal-pad").classList.remove("hidden");
}

function connectMeters() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const ws = new WebSocket(`${proto}//${location.host}/ws/meters`);
  ws.onmessage = (ev) => {
    const data = JSON.parse(ev.data);
    el("meter-mic").style.width = `${Math.min(100, data.mic * 100)}%`;
    el("meter-sb").style.width = `${Math.min(100, data.sb * 100)}%`;
    el("meter-vout").style.width = `${Math.min(100, data.vout * 100)}%`;

    const playing = data.playing || {};
    document.querySelectorAll(".pad").forEach((card) => {
      const pid = card.dataset.padId;
      const pct = playing[pid];
      const prog = el(`prog-${pid}`);
      if (pct !== undefined) {
        card.classList.add("playing");
        if (prog) prog.style.width = `${Math.round(pct * 100)}%`;
      } else {
        card.classList.remove("playing");
        if (prog) prog.style.width = "0%";
      }
    });
  };
  ws.onclose = () => setTimeout(connectMeters, 1500);
}

// Wire controls
el("btn-mic-mute").onclick = async () => {
  const muted = !appState.settings.mic_muted;
  await saveSetting({ mic_muted: muted });
  await refreshState();
};

el("btn-stop-all").onclick = () => api("/api/stop_all", { method: "POST" });
el("btn-add-sound").onclick = () => el("file-input").click();
el("btn-empty-add").onclick = () => el("file-input").click();
el("file-input").onchange = (e) => uploadFiles(e.target.files);

async function importYouTube() {
  const input = el("yt-url-input");
  const status = el("yt-status");
  const url = input.value.trim();
  if (!url) return;
  el("btn-yt-import").disabled = true;
  status.textContent = "Downloading audio...";
  try {
    const res = await api("/api/pads/youtube", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url, auto_play: true }),
    });
    input.value = "";
    status.textContent = res.pad ? `Playing: ${res.pad.name}` : "Added!";
    await refreshState();
  } catch (err) {
    status.textContent = err.message;
  } finally {
    el("btn-yt-import").disabled = false;
  }
}

el("btn-yt-import").onclick = importYouTube;
el("yt-url-input").onkeydown = (e) => {
  if (e.key === "Enter") importYouTube();
};

el("slider-mic-gain").oninput = (e) => {
  const v = parseFloat(e.target.value);
  el("mic-gain-val").textContent = `${Math.round(v * 100)}%`;
  saveSetting({ mic_gain: v });
};

el("slider-sb-gain").oninput = (e) => {
  const v = parseFloat(e.target.value);
  el("sb-gain-val").textContent = `${Math.round(v * 100)}%`;
  saveSetting({ soundboard_gain: v });
};

el("slider-mon-gain").oninput = (e) => {
  const v = parseFloat(e.target.value);
  el("mon-gain-val").textContent = `${Math.round(v * 100)}%`;
  saveSetting({ monitor_gain: v });
};

el("chk-monitor").onchange = (e) => saveSetting({ monitor_enabled: e.target.checked });
el("chk-polyphonic").onchange = (e) => {
  appState.settings.polyphonic = e.target.checked;
  saveSetting({ polyphonic: e.target.checked });
};
el("chk-duck").onchange = (e) => saveSetting({ duck_mic: e.target.checked });
el("chk-duck-music").onchange = (e) => saveSetting({ duck_music_on_voice: e.target.checked });
el("chk-queue").onchange = (e) => saveSetting({ queue_mode: e.target.checked });
el("chk-auto-mic").onchange = async (e) => {
  await saveSetting({ auto_switch_default_mic: e.target.checked });
  await refreshState();
};

// Routing modal
el("btn-routing").onclick = () => el("modal-routing").classList.remove("hidden");
el("close-routing").onclick = () => el("modal-routing").classList.add("hidden");

el("sel-mic").onchange = async (e) => { await saveSetting({ mic_device: e.target.value }); await refreshState(); };
el("sel-vout").onchange = async (e) => { await saveSetting({ virtual_out_device: e.target.value }); await refreshState(); };
el("sel-mon").onchange = async (e) => { await saveSetting({ monitor_out_device: e.target.value }); await refreshState(); };
el("slider-vout-db").oninput = (e) => {
  const v = parseFloat(e.target.value);
  el("vout-db-label").textContent = `${v.toFixed(1)} dB`;
  saveSetting({ virtual_out_gain_db: v });
};

el("btn-calibrate").onclick = async () => {
  el("calibrate-status").textContent = "Probing loopback...";
  try {
    const res = await api("/api/calibrate", { method: "POST" });
    el("calibrate-status").textContent = res.ok
      ? `Calibrated: ${res.recommended_db} dB (driver ${res.driver_gain_linear}x)`
      : res.reason;
    await refreshState();
  } catch (err) {
    el("calibrate-status").textContent = err.message;
  }
};

// Pad Editor modal
el("close-pad").onclick = () => { recordingHotkey = false; el("modal-pad").classList.add("hidden"); };
el("edit-pad-vol").oninput = (e) => {
  el("edit-pad-vol-label").textContent = `${Math.round(parseFloat(e.target.value) * 100)}%`;
};

el("btn-record-hotkey").onclick = () => {
  recordingHotkey = true;
  el("edit-pad-hotkey").value = "Press any key...";
};
el("btn-clear-hotkey").onclick = () => {
  recordingHotkey = false;
  el("edit-pad-hotkey").value = "";
};

window.addEventListener("keydown", (e) => {
  if (!recordingHotkey) return;
  e.preventDefault();
  if (["Control", "Shift", "Alt", "Meta"].includes(e.key)) return;
  const parts = [];
  if (e.ctrlKey) parts.push("ctrl");
  if (e.altKey) parts.push("alt");
  if (e.shiftKey) parts.push("shift");
  let keyName = e.key.toLowerCase();
  if (e.code && e.code.startsWith("Numpad") && e.code.length === 7) {
    keyName = `num${e.code.slice(6)}`;
  }
  parts.push(keyName);
  el("edit-pad-hotkey").value = parts.join("+");
  recordingHotkey = false;
});

el("btn-save-pad").onclick = async () => {
  const pid = el("edit-pad-id").value;
  const startVal = parseFloat(el("edit-pad-start").value);
  const endRaw = el("edit-pad-end").value.trim();
  const endVal = endRaw === "" ? null : parseFloat(endRaw);
  await api(`/api/pads/${pid}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      name: el("edit-pad-name").value.trim() || "Clip",
      volume: parseFloat(el("edit-pad-vol").value),
      hotkey: el("edit-pad-hotkey").value.trim(),
      start_sec: Number.isFinite(startVal) ? Math.max(0, startVal) : 0,
      end_sec: endVal !== null && Number.isFinite(endVal) ? endVal : null,
      loop: el("edit-pad-loop").checked,
      color: el("edit-pad-color").value,
    }),
  });
  el("modal-pad").classList.add("hidden");
  await refreshState();
};

el("btn-delete-pad").onclick = async () => {
  const pid = el("edit-pad-id").value;
  await api(`/api/pads/${pid}`, { method: "DELETE" });
  el("modal-pad").classList.add("hidden");
  await refreshState();
};

// Drag & Drop
window.addEventListener("dragover", (e) => {
  e.preventDefault();
  el("drop-overlay").classList.remove("hidden");
});
window.addEventListener("dragleave", (e) => {
  if (e.relatedTarget === null) el("drop-overlay").classList.add("hidden");
});
window.addEventListener("drop", async (e) => {
  e.preventDefault();
  el("drop-overlay").classList.add("hidden");
  if (e.dataTransfer && e.dataTransfer.files.length) {
    await uploadFiles(e.dataTransfer.files);
  }
});

refreshState();
connectMeters();
