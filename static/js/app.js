/* global L */

// ---------- state ----------
let map, marker;
let selected = { lat: null, lon: null, name: "Unknown" };
let lastResult = null; // server response from /api/plan_trip
let lastFactor = null; // which factor is shown in modal

// ---------- helpers ----------
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

function fmtTimeLocal(iso, tz) {
  try {
    const d = new Date(iso);
    return d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit", timeZone: tz });
  } catch {
    return iso;
  }
}
function fmtRangeLocal(startIso, endIso, tz) {
  try {
    const s = new Date(startIso), e = new Date(endIso);
    const ds = s.toLocaleString([], { weekday: "short", month: "short", day: "numeric", timeZone: tz });
    const ts = s.toLocaleTimeString([], { hour: "numeric", minute: "2-digit", timeZone: tz });
    const te = e.toLocaleTimeString([], { hour: "numeric", minute: "2-digit", timeZone: tz });
    return `${ds} ${ts} — ${te} (${tz})`;
  } catch {
    return `${startIso} — ${endIso} (${tz})`;
  }
}
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
const isNum = (x) => typeof x === "number" && Number.isFinite(x);

function nearestBoundaryDistance(val, lo, hi) {
  if (!isNum(val)) return null;
  const d = [];
  if (isNum(lo)) d.push(Math.abs(val - lo));
  if (isNum(hi)) d.push(Math.abs(val - hi));
  if (!d.length) return null;
  return Math.min(...d);
}

/**
 * Fallback flip proxy (per-hour):
 * p = |local_trend| / distance_to_nearest_threshold
 * + climatology tweak if available (pct_of_normal near 100 => reduce a bit, far => increase a bit)
 */
function fallbackFlipSeries(values, th, climForFactor) {
  // values: array of numbers (UI units)
  // th: {min?, max?} in UI units
  const eps = 1e-6;
  const arr = values || [];
  const out = new Array(arr.length).fill(null);

  // Light climatology tweak parameters
  let tweakUp = 0, tweakDown = 0;
  if (climForFactor && isNum(climForFactor.pct_of_normal)) {
    const pct = climForFactor.pct_of_normal; // 100 = normal
    if (pct > 130 || pct < 70) tweakUp = 0.1;          // unusual -> nudge up
    if (pct <= 110 && pct >= 90) tweakDown = 0.1;      // near normal -> nudge down
  }

  for (let i = 0; i < arr.length; i++) {
    const v = arr[i];
    if (!isNum(v)) { out[i] = null; continue; }
    const prev = i > 0 && isNum(arr[i-1]) ? arr[i-1] : null;
    const next = i+1 < arr.length && isNum(arr[i+1]) ? arr[i+1] : null;

    let slope = 0;
    if (isNum(prev)) slope = Math.max(slope, Math.abs(v - prev));
    if (isNum(next)) slope = Math.max(slope, Math.abs(next - v));

    const dist = nearestBoundaryDistance(v, th?.min, th?.max);
    if (dist === null) { out[i] = null; continue; }

    let p = clamp(slope / (dist + eps), 0, 1);
    if (tweakUp)  p = clamp(p + tweakUp, 0, 1);
    if (tweakDown) p = clamp(p - tweakDown, 0, 1);
    out[i] = p;
  }
  return out;
}
function avgDefined(xs) {
  const vals = (xs || []).filter(isNum);
  if (!vals.length) return null;
  return vals.reduce((a,b) => a+b, 0) / vals.length;
}

function colorFromBoolMix(allTrue, anyFalse, anyUnknown) {
  // red if any false, green only if all true, yellow if mixed/unknown
  if (anyFalse) return "bad";
  if (allTrue) return "good";
  return "warn";
}

function chip(text) {
  const span = document.createElement("span");
  span.className = "metaChip";
  span.textContent = text;
  return span;
}

// ---------- map ----------
function initMap() {
  map = L.map("map").setView([35.0, -106.0], 6);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 18,
    attribution: "&copy; OpenStreetMap",
  }).addTo(map);

  map.on("click", async (e) => {
    selected.lat = e.latlng.lat;
    selected.lon = e.latlng.lng;
    await placeMarker(selected.lat, selected.lon);
    await reverseGeocode(selected.lat, selected.lon);
  });
}
async function placeMarker(lat, lon) {
  if (marker) map.removeLayer(marker);
  marker = L.marker([lat, lon]).addTo(map);
  marker.bindPopup("Unknown").openPopup();
}
async function reverseGeocode(lat, lon) {
  try {
    const r = await fetch("/api/reverse_geocode", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({lat, lon})
    });
    const j = await r.json();
    if (j.ok) {
      selected.name = j.display_name;
      if (marker) marker.setPopupContent(`${selected.name}<br>${lat.toFixed(5)}, ${lon.toFixed(5)}`);
      $("#placeInfo").innerHTML = `
        <div><b>Place:</b> ${selected.name}</div>
        <div><b>Coords:</b> ${lat.toFixed(5)},  ${lon.toFixed(5)}</div>
        <div class="small">Pick a time window below and set your conditions.</div>
      `;
    }
  } catch(e) { /* silent */ }
}

// ---------- UI read ----------
function readPrefs() {
  return {
    consider: {
      precip_prob: $("#chkPProb").checked,
      precip_amt:  $("#chkPAmt").checked,
      temp:        $("#chkTemp").checked,
      humidity:    $("#chkHum").checked,
      wind:        $("#chkWind").checked,
    },
    units: {
      temp: $("#tUnit").value,          // C or F
      wind: $("#wUnit").value,          // mph / m/s / km/h (UI shows mph/m/s; back end converts) 
      precip_amt: $("#pamtUnit").value, // in/mm
    },
    thresholds: {
      precip_prob_max: numOrNull($("#pprobMax").value),
      precip_amt_max:  numOrNull($("#pamtMax").value),
      temp_min: numOrNull($("#tMin").value),
      temp_max: numOrNull($("#tMax").value),
      humidity_min: numOrNull($("#hMin").value),
      humidity_max: numOrNull($("#hMax").value),
      wind_max: numOrNull($("#wMax").value),
      wind_min: numOrNull($("#wMin").value),
    }
  };
}
function numOrNull(v) {
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

// ---------- run planning ----------
$("#runBtn").addEventListener("click", async () => {
  if (selected.lat == null || selected.lon == null) {
    showError("Click on the map to choose a location.");
    return;
  }
  hideError();
  const tzText = $("#tzText").textContent || "UTC";
  const payload = {
    trip_name: $("#tripName").value || "My Trip",
    lat: selected.lat,
    lon: selected.lon,
    data_source: $("#dataSource").value,
    window: {
      start_local: $("#startLocal").value,
      end_local:   $("#endLocal").value
    },
    prefs: readPrefs()
  };
  try {
    const r = await fetch("/api/plan_trip", {
      method: "POST", headers: {"Content-Type":"application/json"},
      body: JSON.stringify(payload)
    });
    const j = await r.json();
    if (!j.ok) {
      showError(j.message || "Plan error.");
      return;
    }
    if (j.no_data_for_window) {
      showError(j.message || "No data available for this window.");
      $("#planResult").innerHTML = "";
      $("#condPanel").style.display = "none";
      $("#suggestPanel").style.display = "none";
      $("#probPanel").style.display = "none";
      lastResult = null;
      return;
    }

    lastResult = j;
    // Top summary
    $("#planResult").innerHTML = `
      <div class="${j.meets ? 'good':'bad'}"><b>${j.summary}</b></div>
      <div class="small">${fmtRangeLocal(j.window.start_local, j.window.end_local, j.timezone)}</div>
      <div class="small">Data source: ${labelForSource(j.data_source)}. Checked factors: ${checkedFactorsList(payload.prefs.consider)}.</div>
    `;

    renderConditions(j);
    renderSuggestions(j);
    renderUncertainty(j);
    await refreshTrips(); // keep trips fresh after a run
  } catch (e) {
    showError("Plan error: " + e);
  }
});

function labelForSource(src) {
  if (src === "nasa-power" || src === "nasa") return "NASA POWER";
  if (src === "combined") return "Combined (OM values + POWER uncertainty)";
  return "Open-Meteo";
}
function checkedFactorsList(c) {
  const m = [];
  if (c.precip_prob) m.push("Precip. Probability");
  if (c.precip_amt)  m.push("Precip. Amount");
  if (c.temp)        m.push("Temperature");
  if (c.humidity)    m.push("Humidity");
  if (c.wind)        m.push("Wind Speed");
  return m.join(", ");
}

function showError(msg) {
  const el = $("#errorPanel");
  el.style.display = "block";
  el.textContent = msg;
}
function hideError() {
  const el = $("#errorPanel");
  el.style.display = "none";
  el.textContent = "";
}

// ---------- render conditions ----------
function renderConditions(j) {
  $("#condPanel").style.display = "block";
  const rows = $("#condRows");
  rows.innerHTML = "";

  // Decide box color per factor: red if any hour fails
  const perHour = j.hourly_factor_ok || []; // array of maps per hour
  const hoursCount = perHour.length;

  function factorColor(key) {
    if (!hoursCount) return "warn";
    let allTrue = true, anyFalse = false, anyUnknown = false;
    for (const m of perHour) {
      if (!(key in m)) { anyUnknown = true; continue; }
      const v = m[key];
      if (v === false) anyFalse = true;
      if (v !== true) allTrue = false;
    }
    return colorFromBoolMix(allTrue, anyFalse, anyUnknown);
  }

  // Build each condition row from server-provided summarization + color using per-hour
  (j.conditions || []).forEach((c) => {
    const div = document.createElement("div");
    div.className = "cond-row";
    div.dataset.key = c.key;

    const b = document.createElement("div");
    b.className = "cond-box";
    b.classList.add(factorColor(c.key)); // coloring rule
    div.appendChild(b);

    const name = document.createElement("div");
    name.className = "cond-name";
    name.textContent = c.label;
    div.appendChild(name);

    const range = document.createElement("div");
    range.className = "cond-range";
    const minTxt = (c.min == null ? "—" : `${c.min} ${c.unit || ""}`.trim());
    const maxTxt = (c.max == null ? "—" : `${c.max} ${c.unit || ""}`.trim());
    range.textContent = `(min: ${minTxt}, max: ${maxTxt})`;
    div.appendChild(range);

    // Climatology chip if present
    if (j.climatology && j.climatology[c.key]) {
      const info = j.climatology[c.key];
      const label = info.label || `${info.pct_of_normal}% of normal`;
      div.appendChild(chip(`vs normal: ${label}`));
    }

    // Open modal on click
    div.addEventListener("click", () => openFactorModal(c.key));
    rows.appendChild(div);
  });
}

function renderSuggestions(j) {
  $("#suggestPanel").style.display = "block";
  const u = $("#unidealSpans");
  const a = $("#altWindows");
  u.innerHTML = "";
  a.innerHTML = "";

  function addSpans(label, spans) {
    if (!spans || !spans.length) return;
    const wrap = document.createElement("div");
    wrap.style.marginBottom = "8px";
    wrap.innerHTML = `<div class="small"><b>${label}:</b></div>`;
    const ul = document.createElement("ul");
    spans.forEach(s => {
      const li = document.createElement("li");
      li.textContent = `${fmtRangeLocal(s.start, s.end, lastResult.timezone)}`;
      ul.appendChild(li);
    });
    wrap.appendChild(ul);
    u.appendChild(wrap);
  }

  const us = j.unideal_spans || {};
  addSpans("Precip. Probability", us.precip_prob);
  addSpans("Precip. Amount",      us.precip_amt);
  addSpans("Temperature",         us.temp);
  addSpans("Humidity",            us.humidity);
  addSpans("Wind Speed",          us.wind);

  if ((j.alt_windows || []).length) {
    const hh = document.createElement("div");
    hh.className = "small";
    hh.style.marginTop = "6px";
    hh.innerHTML = "<b>Consider these nearby windows (same duration):</b>";
    a.appendChild(hh);

    const ul = document.createElement("ul");
    (j.alt_windows || []).forEach(w => {
      const li = document.createElement("li");
      li.textContent = `${w.direction === "before" ? "Before" : "After"}: ${fmtRangeLocal(w.start, w.end, lastResult.timezone)}`;
      ul.appendChild(li);
    });
    a.appendChild(ul);
  }
}

function renderUncertainty(j) {
  $("#probPanel").style.display = "block";
  const rows = $("#probRows");
  rows.innerHTML = "";
  (j.uncertainty || []).forEach(u => {
    const div = document.createElement("div");
    div.className = "cond-row";
    const box = document.createElement("div");
    box.className = "prob-box";
    if (u.flip_prob == null) box.classList.add("warn");
    else if (u.flip_prob >= 0.7) box.classList.add("bad");
    else if (u.flip_prob >= 0.3) box.classList.add("warn");
    else box.classList.add("good");
    div.appendChild(box);

    const name = document.createElement("div");
    name.className = "prob-name";
    name.textContent = u.label;
    div.appendChild(name);

    const txt = document.createElement("div");
    txt.className = "prob-text";
    txt.textContent = "Chance to flip outcome: " + (u.flip_prob == null ? "—" : `${Math.round(u.flip_prob*100)}%`);
    div.appendChild(txt);

    rows.appendChild(div);
  });
}

// ---------- factor modal ----------
const modal = $("#modalOverlay");
$("#closeModal").addEventListener("click", () => { modal.style.display = "none"; });
$("#downloadCSV").addEventListener("click", () => {
  if (!lastResult || !lastFactor) return;
  const url = `/api/trip/${lastResult.trip_id}/csv?factor=${encodeURIComponent(lastFactor)}&with_summary=1`;
  window.open(url, "_blank");
});

function openFactorModal(key) {
  if (!lastResult) return;
  lastFactor = key;
  const labelMap = {
    "temp": "Temperature",
    "humidity": "Humidity",
    "wind": "Wind Speed",
    "precip_amt": "Precip. Amount",
    "precip_prob": "Precip. Probability",
  };
  $("#factorTitle").textContent = labelMap[key] || key;

  $("#factorSubtitle").textContent = fmtRangeLocal(
    lastResult.window.start_local,
    lastResult.window.end_local,
    lastResult.timezone
  );

  // meta chips: ideal + observed min/max + avg flip risk
  const prefs = lastResult.prefs || {};
  const th = prefs.thresholds || {};
  const units = prefs.units || {};
  const cond = (lastResult.conditions || []).find(c => c.key === key);
  const meta = $("#factorMeta");
  meta.innerHTML = "";

  // show ideal thresholds in UI units
  const unitMap = { temp: units.temp || "C", wind: units.wind || "m/s", precip_amt: units.precip_amt || "mm", precip_prob: "%", humidity: "%" };
  if (key === "temp") meta.appendChild(chip(`Ideal: ${fmtRange(th.temp_min, th.temp_max, unitMap.temp)}`));
  if (key === "wind") meta.appendChild(chip(`Ideal: ≤ ${fmtOne(th.wind_max, unitMap.wind)}` + (isNum(th.wind_min) ? `, ≥ ${fmtOne(th.wind_min, unitMap.wind)}` : "")));
  if (key === "precip_amt") meta.appendChild(chip(`Ideal: ≤ ${fmtOne(th.precip_amt_max, unitMap.precip_amt)}`));
  if (key === "precip_prob") meta.appendChild(chip(`Ideal: ≤ ${fmtOne(th.precip_prob_max, "%")}`));
  if (key === "humidity") meta.appendChild(chip(`Ideal: ${fmtRange(th.humidity_min, th.humidity_max, "%")}`));

  // observed range
  if (cond) meta.appendChild(chip(`Observed (min: ${fmtOne(cond.min, cond.unit)}, max: ${fmtOne(cond.max, cond.unit)})`));

  // climatology chip if present
  if (lastResult.climatology && lastResult.climatology[key]) {
    const c = lastResult.climatology[key];
    meta.appendChild(chip(`vs normal: ${c.label || (c.pct_of_normal + "%")}`));
  }

  // determine per-hour values to display
  const s = lastResult.series || {};
  const times = s.time || [];
  const perHourOK = lastResult.hourly_factor_ok || [];
  const perHourReasons = lastResult.hourly || [];
  const tbody = $("#factorTBody");
  tbody.innerHTML = "";

  // column values per factor
  let values = null, unit = "";
  if (key === "temp") { values = s.temp || []; unit = unitMap.temp; }
  else if (key === "humidity") { values = s.humidity || []; unit = "%"; }
  else if (key === "wind") { values = s.wind || []; unit = unitMap.wind; }
  else if (key === "precip_amt") { values = s.precip_amt || []; unit = unitMap.precip_amt; }
  else if (key === "precip_prob") { values = s.precip_prob || []; unit = "%"; }

  // per-hour flip probabilities: prefer server; else compute fallback
  const serverFlip = (lastResult.hourly_flip || []).map(m => (m ? m[key] : null));
  let flipSeries = serverFlip;
  if (!serverFlip.some(isNum)) {
    // build thresholds in UI units for fallback
    const thUI = { min: null, max: null };
    if (key === "temp") { thUI.min = th.temp_min; thUI.max = th.temp_max; }
    if (key === "humidity") { thUI.min = th.humidity_min; thUI.max = th.humidity_max; }
    if (key === "wind") { thUI.min = th.wind_min; thUI.max = th.wind_max; }
    if (key === "precip_amt") { thUI.max = th.precip_amt_max; }
    // inject climatology info if available
    const clim = lastResult.climatology ? lastResult.climatology[key] : null;
    flipSeries = fallbackFlipSeries(values, thUI, clim);
  }

  // avg flip risk chip
  const avgFlip = avgDefined(flipSeries);
  meta.appendChild(chip(`Avg flip risk: ${avgFlip == null ? "—" : Math.round(avgFlip*100) + "%"}`));

  // table rows
  for (let i = 0; i < times.length; i++) {
    const tr = document.createElement("tr");
    const tCell = document.createElement("td");
    tCell.textContent = fmtTimeLocal(times[i], lastResult.timezone);
    tr.appendChild(tCell);

    const vCell = document.createElement("td");
    vCell.textContent = values && isNum(values[i]) ? values[i] : "—";
    tr.appendChild(vCell);

    const uCell = document.createElement("td");
    uCell.textContent = unit || "";
    tr.appendChild(uCell);

    const idealCell = document.createElement("td");
    const okMap = perHourOK[i] || {};
    const ideal = okMap[key];
    idealCell.innerHTML = ideal == null ? "—" : (ideal ? "<span class='good'>Yes</span>" : "<span class='bad'>No</span>");
    tr.appendChild(idealCell);

    const flipCell = document.createElement("td");
    const fp = flipSeries[i];
    flipCell.textContent = isNum(fp) ? `${Math.round(fp*100)}%` : "—";
    tr.appendChild(flipCell);

    const overallCell = document.createElement("td");
    const row = perHourReasons[i] || {};
    overallCell.innerHTML = row.ok ? "<span class='good'>OK</span>" : "<span class='bad'>No-Go</span>";
    tr.appendChild(overallCell);

    const reasonCell = document.createElement("td");
    const reasons = row.reasons || [];
    reasonCell.textContent = reasons.join(", ");
    tr.appendChild(reasonCell);

    tbody.appendChild(tr);
  }

  // show modal
  modal.style.display = "flex";
}

function fmtOne(v, unit) {
  if (v == null || v === "") return "—";
  return `${v} ${unit || ""}`.trim();
}
function fmtRange(lo, hi, unit) {
  const L = lo == null ? "—" : `${lo} ${unit||""}`.trim();
  const H = hi == null ? "—" : `${hi} ${unit||""}`.trim();
  return `${L} — ${H}`;
}

// ---------- trips ----------
async function refreshTrips() {
  try {
    const r = await fetch("/api/trips");
    const j = await r.json();
    const box = $("#tripsList");
    if (!j.ok) { box.textContent = "Error loading trips."; return; }
    const trips = (j.trips || []);
    if (!trips.length) { box.textContent = "No trips yet."; return; }
    box.innerHTML = "";
    trips.forEach(t => {
      const div = document.createElement("div");
      div.className = "trip";
      const left = document.createElement("div");
      left.innerHTML = `
        <div><b>${t.name || "Untitled Trip"}</b> ${t.last_result?.meets ? "<span class='good'>Go</span>" : "<span class='bad'>No-Go</span>"}</div>
        <div class="small">${fmtRangeLocal(t.window.start_local, t.window.end_local, t.timezone)}</div>
      `;
      div.appendChild(left);

      const right = document.createElement("div");
      const viewBtn = document.createElement("button");
      viewBtn.className = "secondary";
      viewBtn.textContent = "View";
      viewBtn.addEventListener("click", () => openSavedTrip(t.id));
      right.appendChild(viewBtn);

      const delBtn = document.createElement("button");
      delBtn.className = "secondary";
      delBtn.style.marginLeft = "6px";
      delBtn.textContent = "Delete";
      delBtn.addEventListener("click", async () => {
        await fetch("/api/trips/delete", { method: "POST", headers: {"Content-Type":"application/json"}, body: JSON.stringify({id: t.id}) });
        refreshTrips();
      });
      right.appendChild(delBtn);

      div.appendChild(right);
      box.appendChild(div);
    });
  } catch (e) {
    $("#tripsList").textContent = "Error loading trips.";
  }
}
$("#refreshTrips").addEventListener("click", refreshTrips);

async function openSavedTrip(id) {
  try {
    const r = await fetch(`/api/trip/${id}`);
    const j = await r.json();
    if (!j.ok) { showError("Trip not found."); return; }
    lastResult = j.trip;
    // Render panels from saved trip
    $("#planResult").innerHTML = `
      <div class="${lastResult.meets ? 'good':'bad'}"><b>${lastResult.summary}</b></div>
      <div class="small">${fmtRangeLocal(lastResult.window.start_local, lastResult.window.end_local, lastResult.timezone)}</div>
      <div class="small">Data source: ${labelForSource(lastResult.data_source)}.</div>
    `;
    renderConditions(lastResult);
    renderSuggestions(lastResult);
    renderUncertainty(lastResult);
  } catch(e) {
    showError("Error loading saved trip.");
  }
}

// ---------- initial ----------
document.addEventListener("DOMContentLoaded", async () => {
  initMap();
  // Set default time window (today 10:00–13:00 local)
  const now = new Date();
  const start = new Date(now.getFullYear(), now.getMonth(), now.getDate(), 10, 0, 0);
  const end = new Date(now.getFullYear(), now.getMonth(), now.getDate(), 13, 0, 0);
  $("#startLocal").value = toLocalInput(start);
  $("#endLocal").value = toLocalInput(end);
  // attempt timezone badge update once a location is picked via /api/hourly
  $("#tzText").textContent = "UTC";
  $("#tzText2").textContent = "UTC";
  await refreshTrips();
});

function toLocalInput(d) {
  const pad = (n) => String(n).padStart(2,"0");
  return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
