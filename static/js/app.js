// Trip Planner front-end logic (Leaflet map + planner UI)

// ---------------------- error utilities ----------------------
const errorPanel = document.getElementById("errorPanel");
function showError(msg) {
  if (errorPanel) {
    errorPanel.style.display = "block";
    errorPanel.textContent = "Error: " + msg;
  }
  console.error("[TripPlanner]", msg);
}

async function fetchJSON(url, opts) {
  const r = await fetch(url, opts);
  const ct = r.headers.get("content-type") || "";
  if (!ct.includes("application/json")) {
    const text = await r.text();
    throw new Error(`Non-JSON response (${r.status}): ${text.slice(0, 200)}`);
  }
  const json = await r.json();
  return { status: r.status, json };
}

// ---------------------- coordinate helpers ----------------------
function normalizeCoords(lat, lon) {
  let la = Math.max(-90, Math.min(90, Number(lat)));
  let lo = ((Number(lon) + 180) % 360) - 180;
  return { lat: la, lon: lo };
}

// ---------------------- map ----------------------
let map;
try {
  map = L.map("map", { worldCopyJump: true }).setView([20, 0], 2);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: "© OpenStreetMap"
  }).addTo(map);
} catch (e) { showError("Leaflet failed to load: " + e.message); }

// ---------------------- DOM refs ----------------------
const placeInfo  = document.getElementById("placeInfo");
const tzBadge    = document.getElementById("tzBadge");
const tzText     = document.getElementById("tzText");
const planResult = document.getElementById("planResult");
const tripsList  = document.getElementById("tripsList");

const condPanel     = document.getElementById("condPanel");
const condRows      = document.getElementById("condRows");
const suggestPanel  = document.getElementById("suggestPanel");
const unidealSpans  = document.getElementById("unidealSpans");
const altWindowsDiv = document.getElementById("altWindows");

let lastMarker = null;
let current = { lat: null, lon: null, name: null, tz: null };

// ---------------------- reverse geocode & tz ----------------------
async function reverseGeocode(lat, lon) {
  try {
    const { json } = await fetchJSON("/api/reverse_geocode", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ lat, lon })
    });
    if (json.ok) return json.display_name || json.name || "Unknown";
  } catch (e) { showError("Reverse geocode failed: " + e.message); }
  return "Unknown";
}

async function fetchHourly(lat, lon) {
  const { json } = await fetchJSON(`/api/hourly?lat=${lat}&lon=${lon}`);
  if (!json.ok) throw new Error(json.error || "hourly failed");
  return json.data;
}

async function setTimezoneBadge(lat, lon) {
  try {
    const data = await fetchHourly(lat, lon);
    const tz = data.timezone || "UTC";
    current.tz = tz;
    tzBadge.textContent = `Timezone: ${tz}`;
    tzText.textContent = tz;
  } catch (e) {
    current.tz = "UTC";
    tzBadge.textContent = "Timezone: UTC";
    tzText.textContent = "UTC";
    showError("Could not fetch timezone; using UTC. " + e.message);
  }
}

function setPlaceCard(name, lat, lon) {
  placeInfo.innerHTML = `
    <div class="kv"><b>Place:</b> ${name}</div>
    <div class="kv"><b>Coords:</b> <span class="mono">${lat.toFixed(5)}, ${lon.toFixed(5)}</span></div>
    <div class="small">Pick a time window below and set your conditions.</div>
  `;
}

// ---------------------- map click ----------------------
if (map) {
  map.on("click", async (e) => {
    const norm = normalizeCoords(e.latlng.lat, e.latlng.lng);
    const lat = norm.lat, lon = norm.lon;

    if (lastMarker) map.removeLayer(lastMarker);
    lastMarker = L.marker([e.latlng.lat, e.latlng.lng]).addTo(map);

    const name = await reverseGeocode(lat, lon);
    await setTimezoneBadge(lat, lon);
    current = { lat, lon, name, tz: current.tz };

    lastMarker.bindPopup(`${name}<br>${lat.toFixed(4)}, ${lon.toFixed(4)}`).openPopup();
    setPlaceCard(name, lat, lon);
  });
}

// ---------------------- inputs ----------------------
function val(id){ const el=document.getElementById(id); return el?el.value:""; }
function num(id){ const v=val(id); if(v===""||v==null) return null; const n=Number(v); return Number.isFinite(n)?n:null; }
function chk(id){ const el=document.getElementById(id); return !!(el && el.checked); }

// ---------------------- formatting ----------------------
function fmtDateTimeRange(startISO, endISO, tz) {
  const fDate = new Intl.DateTimeFormat([], { timeZone: tz, weekday:"short", year:"numeric", month:"short", day:"numeric" });
  const fTime = new Intl.DateTimeFormat([], { timeZone: tz, hour:"numeric", minute:"2-digit" });
  const s = new Date(startISO), e = new Date(endISO);
  return `${fDate.format(s)} ${fTime.format(s)} — ${fTime.format(e)} (${tz})`;
}
function fmtRange(startISO, endISO, tz) { return fmtDateTimeRange(startISO, endISO, tz); }
function fmtHour(iso, tz) {
  const f = new Intl.DateTimeFormat([], { timeZone: tz, hour:"numeric", minute:"2-digit" });
  return f.format(new Date(iso));
}
function prettyMinMax(min, max, unit) {
  const u = unit ? ` ${unit}` : "";
  const m1 = (min ?? "") === "" || min === null ? "—" : min;
  const m2 = (max ?? "") === "" || max === null ? "—" : max;
  return `(min: ${m1}${u}, max: ${m2}${u})`;
}

// ---------------------- UI renderers ----------------------
function renderTimeline(hourly, tz) {
  if (!Array.isArray(hourly) || !hourly.length) return "";
  const ticks = hourly.map(h => {
    const cls = h.ok ? "tick" : "tick bad";
    const label = `${fmtHour(h.time, tz)} — ${h.ok ? "OK" : "No-Go: " + (h.reasons || []).join(", ")}`;
    return `<div class="${cls}" title="${label}"></div>`;
  }).join("");
  return `<div class="timeline">${ticks}</div>`;
}

function renderConditions(conditions) {
  if (!Array.isArray(conditions) || !conditions.length) {
    condPanel.style.display = "none";
    condRows.innerHTML = "";
    return;
  }
  const rows = conditions.map(c => {
    const cls = c.ok ? "cond-box" : "cond-box bad";
    const unit = c.unit || "";
    const range = prettyMinMax(c.min, c.max, unit);
    return `
      <div class="cond-row">
        <div class="${cls}"></div>
        <div class="cond-name">${c.label}</div>
        <div class="cond-range">${range}</div>
      </div>
    `;
  }).join("");
  condRows.innerHTML = rows;
  condPanel.style.display = "block";
}

function renderSuggestions(res) {
  const tz = res.timezone || "UTC";
  const spans = res.unideal_spans || {};
  const alts  = res.alt_windows || [];

  const hasSpans = Object.keys(spans).some(k => Array.isArray(spans[k]) && spans[k].length);
  const hasAlts  = Array.isArray(alts) && alts.length;

  if (!hasSpans && !hasAlts) {
    suggestPanel.style.display = "none";
    unidealSpans.innerHTML = "";
    altWindowsDiv.innerHTML = "";
    return;
  }

  let spanHtml = "";
  if (hasSpans) {
    spanHtml += `<div class="small"><b>Unideal spans around your selection (by factor):</b></div>`;
    for (const [key, items] of Object.entries(spans)) {
      if (!items || !items.length) continue;
      const label = {
        precip_prob: "Precip. Probability",
        precip_amt: "Precip. Amount",
        temp: "Temperature",
        humidity: "Humidity",
        wind: "Wind Speed"
      }[key] || key;

      const list = items
        .map(s => `• ${fmtDateTimeRange(s.start, s.end, tz)}`)
        .join("<br>");
      spanHtml += `<div class="suggest-item"><span style="font-weight:600">${label}:</span><br>${list}</div>`;
    }
  }
  unidealSpans.innerHTML = spanHtml || "";

  let altHtml = "";
  if (hasAlts) {
    altHtml += `<div class="small"><b>Consider these nearby windows (same duration):</b></div>`;
    alts.forEach(a => {
      const dir = a.direction === "before" ? "Before" : "After";
      altHtml += `<div class="suggest-item">• ${dir}: ${fmtDateTimeRange(a.start, a.end, tz)}</div>`;
    });
  }
  altWindowsDiv.innerHTML = altHtml || "";

  suggestPanel.style.display = "block";
}

function renderReport(res) {
  const tz = res.timezone || "UTC";
  const header = `<div class="${res.meets ? "good" : "bad"}"><b>${res.summary}</b></div>`;

  let windowLine = "";
  if (Array.isArray(res.hourly) && res.hourly.length) {
    const startISO = res.hourly[0].time;
    const endISO   = res.hourly[res.hourly.length-1].time;
    windowLine = `<div class="small mono">${fmtDateTimeRange(startISO, endISO, tz)}</div>`;
  }

  let considered = "";
  if (Array.isArray(res.conditions) && res.conditions.length) {
    const labels = res.conditions.map(c => c.label).join(", ");
    considered = `<div class="small">Checked factors: ${labels}</div>`;
  }

  const timelineHTML = renderTimeline(res.hourly || [], tz);

  // Optional “fails” summary (still useful)
  let vhtml = "";
  if (!res.meets && Array.isArray(res.violations)) {
    vhtml += `<h3>Where/When it fails</h3><ul class="small">`;
    res.violations.forEach(v => {
      vhtml += `<li>${fmtDateTimeRange(v.start, v.end, tz)} — see boxes below</li>`;
    });
    vhtml += `</ul>`;
  }

  planResult.innerHTML = header + windowLine + considered + timelineHTML + vhtml;

  renderConditions(res.conditions || []);
  renderSuggestions(res);
}

// ---------------------- Run button ----------------------
document.getElementById("runBtn").addEventListener("click", async () => {
  if (current.lat == null || current.lon == null) {
    planResult.innerHTML = `<span class="bad">Click the map to choose a location first.</span>`;
    return;
  }
  const start_local = val("startLocal");
  const end_local   = val("endLocal");
  if (!start_local || !end_local) {
    planResult.innerHTML = `<span class="bad">Provide a start and end time.</span>`;
    return;
  }

  const body = {
    trip_name: val("tripName") || "Untitled Trip",
    lat: current.lat, lon: current.lon,
    window: { start_local, end_local },
    prefs: {
      consider: {
        precip_prob: chk("chkPProb"),
        precip_amt:  chk("chkPAmt"),
        temp:        chk("chkTemp"),
        humidity:    chk("chkHum"),
        wind:        chk("chkWind"),
      },
      units: {
        temp:       document.getElementById("tUnit").value,
        wind:       document.getElementById("wUnit").value,
        precip_amt: document.getElementById("pamtUnit").value,
      },
      thresholds: {
        precip_prob_max: num("pprobMax"),
        precip_amt_max:  num("pamtMax"),
        temp_min:        num("tMin"),
        temp_max:        num("tMax"),
        humidity_min:    num("hMin"),
        humidity_max:    num("hMax"),
        wind_min:        num("wMin"),
        wind_max:        num("wMax"),
      }
    }
  };

  planResult.textContent = "Evaluating…";
  try {
    const { json } = await fetchJSON("/api/plan_trip", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    });
    if (!json.ok) {
      showError(json.error || "Plan failed");
      planResult.innerHTML = `<span class="bad">${json.error || "Plan failed"}</span>`;
      condPanel.style.display = "none";
      suggestPanel.style.display = "none";
      return;
    }
    if (json.no_data_for_window) {
      planResult.innerHTML = `<span class="bad">${json.message}</span>`;
      condPanel.style.display = "none";
      suggestPanel.style.display = "none";
      return;
    }
    renderReport(json);
    await refreshTrips();
  } catch (e) {
    showError("Plan error: " + e.message);
    planResult.innerHTML = `<span class="bad">${e.message}</span>`;
  }
});

// ---------------------- Trips list ----------------------
async function refreshTrips() {
  try {
    const { json } = await fetchJSON("/api/trips");
    if (!json.ok) { tripsList.textContent = "Failed to load."; return; }
    const trips = json.trips || [];
    if (!trips.length) { tripsList.textContent = "No trips yet."; return; }
    tripsList.innerHTML = trips.map(t => {
      const go = t.last_result?.meets;
      const badge = go ? `<span class="pill" style="background:#e8f7ee;border-color:#cdebd8;color:#0a7c2e;">Go</span>`
                       : `<span class="pill" style="background:#fee2e2;border-color:#fecaca;color:#b00020;">No-Go</span>`;
      return `
        <div class="trip">
          <div>
            <div><b>${t.name}</b> ${badge}</div>
            <div class="small mono">${t.window?.start_local ?? "—"} → ${t.window?.end_local ?? "—"} (${t.timezone || "—"})</div>
            <div class="small">Lat/Lon: ${t.coords?.lat?.toFixed?.(3) ?? t.coords?.lat}, ${t.coords?.lon?.toFixed?.(3) ?? t.coords?.lon}</div>
          </div>
          <div><button class="secondary" onclick="deleteTrip('${t.id}')">Delete</button></div>
        </div>
      `;
    }).join("");
  } catch (e) {
    showError("Load trips error: " + e.message);
    tripsList.textContent = "Error loading trips.";
  }
}
document.getElementById("refreshTrips").addEventListener("click", refreshTrips);
window.deleteTrip = async function(id){
  try{
    const { json } = await fetchJSON("/api/trips/delete", {
      method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({id})
    });
    if (json.ok) refreshTrips(); else showError(json.error || "Delete failed");
  }catch(e){ showError("Delete failed: " + e.message); }
};

// initial
refreshTrips();
