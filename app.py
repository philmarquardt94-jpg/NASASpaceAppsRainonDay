from __future__ import annotations
from flask import Flask, request, render_template
import json, requests, traceback, os
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import List, Dict, Any, Tuple, Optional

app = Flask(__name__, static_folder="static", template_folder="templates")

TRIPS_PATH = Path("trips.json")
USER_AGENT = "TripPlanner/1.0 (contact: you@example.com)"  # change to yours


# ---------- tiny file “DB” ----------
def load_trips() -> List[Dict[str, Any]]:
    if TRIPS_PATH.exists():
        try:
            return json.loads(TRIPS_PATH.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []

def save_trips(trips: List[Dict[str, Any]]) -> None:
    TRIPS_PATH.write_text(json.dumps(trips, indent=2), encoding="utf-8")


# ---------- coord normalization ----------
def normalize_coords(lat: float, lon: float) -> Tuple[float, float]:
    if lat is None or lon is None:
        return lat, lon
    lat = max(-90.0, min(90.0, float(lat)))       # clamp latitude
    lon = ((float(lon) + 180.0) % 360.0) - 180.0  # wrap longitude into [-180, 180)
    return lat, lon


# ---------- pages ----------
@app.get("/", endpoint="home_page")
def home():
    return render_template("index.html")

@app.get("/health", endpoint="health_check")
def health():
    return {"ok": True}


# ---------- reverse geocode ----------
@app.post("/api/reverse_geocode", endpoint="reverse_geocode_api")
def reverse_geocode():
    try:
        data = request.get_json(force=True) or {}
        lat, lon = data.get("lat"), data.get("lon")
        lat, lon = normalize_coords(lat, lon)
        if lat is None or lon is None:
            return {"ok": False, "error": "lat/lon required"}, 400

        r = requests.get(
            "https://nominatim.openstreetmap.org/reverse",
            params={"format": "jsonv2", "lat": lat, "lon": lon, "zoom": 10, "addressdetails": 1},
            headers={"User-Agent": USER_AGENT},
            timeout=12
        )
        r.raise_for_status()
        j = r.json()
        addr = j.get("address", {}) if isinstance(j, dict) else {}
        name = (
            addr.get("city") or addr.get("town") or addr.get("village") or
            addr.get("hamlet") or addr.get("county") or addr.get("state") or
            addr.get("country") or "Unknown"
        )
        return {"ok": True, "name": name, "display_name": j.get("display_name", name)}
    except Exception as e:
        return {"ok": False, "error": f"reverse_geocode failed: {e}"}, 200


# ---------- weather (hourly) ----------
def fetch_hourly(lat: float, lon: float) -> Dict[str, Any]:
    lat, lon = normalize_coords(lat, lon)
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": ",".join([
            "temperature_2m",
            "relative_humidity_2m",
            "wind_speed_10m",
            "precipitation_probability",
            "precipitation",
            # extras available (not yet surfaced in UI)
            "apparent_temperature",
            "cloud_cover",
            "wind_gusts_10m",
        ]),
        "forecast_days": 16,
        "timezone": "auto",
    }
    r = requests.get("https://api.open-meteo.com/v1/forecast", params=params, timeout=15)
    r.raise_for_status()
    return r.json()

@app.get("/api/hourly", endpoint="hourly_forecast")
def hourly():
    try:
        lat = request.args.get("lat", type=float)
        lon = request.args.get("lon", type=float)
        lat, lon = normalize_coords(lat, lon)
        if lat is None or lon is None:
            return {"ok": False, "error": "lat/lon required"}, 400
        data = fetch_hourly(lat, lon)
        return {"ok": True, "data": data}
    except Exception as e:
        return {"ok": False, "error": f"hourly failed: {e}"}, 200


# ---------- helpers ----------
def parse_local_to_tz(iso_local: str, tz_name: str) -> datetime:
    """Take 'YYYY-MM-DDTHH:MM' and attach the given tz as an aware datetime."""
    dt = datetime.strptime(iso_local, "%Y-%m-%dT%H:%M")
    return dt.replace(tzinfo=ZoneInfo(tz_name))

def ensure_aware(dt: datetime, tz_name: str) -> datetime:
    """Ensure a datetime is timezone-aware; if naive, attach the tz."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=ZoneInfo(tz_name))
    return dt

def c_to_f(v):   return None if v is None else (v*9/5 + 32)
def ms_to_mph(v):return None if v is None else (v*2.236936)
def mm_to_in(v): return None if v is None else (v/25.4)

def series_minmax(vals: List[Optional[float]]) -> Tuple[Optional[float], Optional[float]]:
    filt = [v for v in vals if v is not None]
    if not filt:
        return None, None
    return (min(filt), max(filt))

def group_ranges(times: List[datetime], mask: List[bool]) -> List[Dict[str, Any]]:
    """Group contiguous True values in mask into [{start,end}] using times list."""
    out = []
    i, n = 0, len(times)
    while i < n:
        if not mask[i]:
            i += 1
            continue
        start = times[i]
        j = i + 1
        while j < n and mask[j]:
            j += 1
        end = times[j-1]
        out.append({"start": start.isoformat(), "end": end.isoformat()})
        i = j
    return out

def group_violations(times: List[datetime], flags: List[List[str]]) -> List[Dict[str, Any]]:
    """Contiguous hours with ANY violation (union of factors)."""
    mask = [len(f) > 0 for f in flags]
    return group_ranges(times, mask)

def map_factor(flag: str) -> str:
    """Map granular flags to UI factors."""
    if flag in ("temp_low", "temp_high"): return "temp"
    if flag in ("humidity_low", "humidity_high"): return "humidity"
    if flag in ("wind_low", "wind_high"): return "wind"
    if flag == "precip_prob": return "precip_prob"
    if flag == "precip_amt": return "precip_amt"
    return flag


# ---------- plan a trip ----------
@app.post("/api/plan_trip", endpoint="plan_trip_api")
def plan_trip_api():
    try:
        body = request.get_json(force=True) or {}
        lat, lon = body.get("lat"), body.get("lon")
        lat, lon = normalize_coords(lat, lon)
        trip_name = (body.get("trip_name") or "").strip() or "Untitled Trip"
        win = body.get("window") or {}
        start_local, end_local = win.get("start_local"), win.get("end_local")
        prefs = body.get("prefs") or {}

        if lat is None or lon is None or not start_local or not end_local:
            return {"ok": False, "error": "lat, lon, start_local, end_local required"}, 400

        wx = fetch_hourly(float(lat), float(lon))

        tz = wx.get("timezone") or "UTC"
        hourly = wx.get("hourly", {})
        times = hourly.get("time", [])
        temp_c = hourly.get("temperature_2m", [])
        rh = hourly.get("relative_humidity_2m", [])
        wind_ms = hourly.get("wind_speed_10m", [])
        pprob = hourly.get("precipitation_probability", [])
        p_mm = hourly.get("precipitation", [])

        # interpret window (aware)
        start_dt = parse_local_to_tz(start_local, tz)
        end_dt   = parse_local_to_tz(end_local, tz)

        # build extended range (±6h) for context
        all_times: List[datetime] = []
        for tstr in times:
            try:
                t = datetime.fromisoformat(tstr)
            except ValueError:
                t = datetime.strptime(tstr, "%Y-%m-%dT%H:%M")
            all_times.append(ensure_aware(t, tz))

        if not all_times:
            return {"ok": False, "error": "No forecast times returned."}

        pad = timedelta(hours=6)
        ext_start = start_dt - pad
        ext_end   = end_dt + pad

        sel_idxs: List[int] = [i for i, t in enumerate(all_times) if start_dt <= t <= end_dt]
        ext_idxs: List[int] = [i for i, t in enumerate(all_times) if ext_start <= t <= ext_end]

        if not sel_idxs:
            return {
                "ok": True,
                "no_data_for_window": True,
                "message": "No hourly forecast available for that time range (too far out).",
                "timezone": tz
            }

        consider = prefs.get("consider", {})
        units    = prefs.get("units", {})
        th       = prefs.get("thresholds", {})

        use_f   = (units.get("temp") == "F")
        use_mph = (units.get("wind") == "mph")
        use_in  = (units.get("precip_amt") == "in")

        # value accessors with unit conversion
        def temp_at(i): return c_to_f(temp_c[i]) if use_f else temp_c[i]
        def wind_at(i): return ms_to_mph(wind_ms[i]) if use_mph else wind_ms[i]
        def pamt_at(i): return mm_to_in(p_mm[i]) if use_in else p_mm[i]
        def rh_at(i):   return rh[i]
        def ppr_at(i):  return pprob[i]

        # ---- evaluate selected window ----
        sel_times = [all_times[i] for i in sel_idxs]
        hour_flags: List[List[str]] = []

        # also collect series values for observed min/max
        temp_vals  = []
        wind_vals  = []
        pamt_vals  = []
        rh_vals    = []
        pprob_vals = []

        for i in sel_idxs:
            flags: List[str] = []

            if consider.get("precip_prob"):
                ppmx = th.get("precip_prob_max"); v = ppr_at(i)
                pprob_vals.append(v)
                if ppmx is not None and v is not None and float(v) > float(ppmx):
                    flags.append("precip_prob")

            if consider.get("precip_amt"):
                pamx = th.get("precip_amt_max"); v = pamt_at(i)
                pamt_vals.append(v)
                if pamx is not None and v is not None and float(v) > float(pamx):
                    flags.append("precip_amt")

            if consider.get("temp"):
                tmin = th.get("temp_min"); tmax = th.get("temp_max"); v = temp_at(i)
                temp_vals.append(v)
                if v is not None:
                    if tmin is not None and float(v) < float(tmin): flags.append("temp_low")
                    if tmax is not None and float(v) > float(tmax): flags.append("temp_high")

            if consider.get("humidity"):
                hmin = th.get("humidity_min"); hmax = th.get("humidity_max"); v = rh_at(i)
                rh_vals.append(v)
                if v is not None:
                    if hmin is not None and float(v) < float(hmin): flags.append("humidity_low")
                    if hmax is not None and float(v) > float(hmax): flags.append("humidity_high")

            if consider.get("wind"):
                wmin = th.get("wind_min"); wmax = th.get("wind_max"); v = wind_at(i)
                wind_vals.append(v)
                if v is not None:
                    if wmin is not None and float(v) < float(wmin): flags.append("wind_low")
                    if wmax is not None and float(v) > float(wmax): flags.append("wind_high")

            hour_flags.append(flags)

        # per-hour evaluation for timeline UI
        hourly_eval = [
            {"time": sel_times[i].isoformat(), "ok": (len(hour_flags[i]) == 0), "reasons": hour_flags[i]}
            for i in range(len(sel_times))
        ]

        # group contiguous ANY-factor violations within selection
        violations = group_violations(sel_times, hour_flags)
        meets = len(violations) == 0

        # ---- condition summary (observed min/max from data, not thresholds) ----
        cond_summary = []
        def add_cond(key, label, unit, vals, any_bad):
            if not consider.get(key): return
            vmin, vmax = series_minmax(vals)
            cond_summary.append({
                "key": key, "label": label, "ok": (not any_bad),
                "min": None if vmin is None else round(float(vmin), 2),
                "max": None if vmax is None else round(float(vmax), 2),
                "unit": unit
            })

        any_bad = lambda k: any(map_factor(f) == k for fl in hour_flags for f in fl)
        add_cond("precip_prob", "Precip. Probability", "%",  pprob_vals, any_bad("precip_prob"))
        add_cond("precip_amt",  "Precip. Amount",    (units.get("precip_amt") or ""), pamt_vals, any_bad("precip_amt"))
        add_cond("temp",        "Temperature",       (units.get("temp") or ""),        temp_vals, any_bad("temp"))
        add_cond("humidity",    "Humidity",          "%",                              rh_vals,   any_bad("humidity"))
        add_cond("wind",        "Wind Speed",        (units.get("wind") or ""),        wind_vals, any_bad("wind"))

        # ---- EXTENDED ANALYSIS (unideal spans with full date+time) ----
        ext_times = [all_times[i] for i in ext_idxs]
        ext_flags: List[List[str]] = []
        for i in ext_idxs:
            flags: List[str] = []
            if consider.get("precip_prob"):
                ppmx = th.get("precip_prob_max"); v = ppr_at(i)
                if ppmx is not None and v is not None and float(v) > float(ppmx):
                    flags.append("precip_prob")
            if consider.get("precip_amt"):
                pamx = th.get("precip_amt_max"); v = pamt_at(i)
                if pamx is not None and v is not None and float(v) > float(pamx):
                    flags.append("precip_amt")
            if consider.get("temp"):
                tmin = th.get("temp_min"); tmax = th.get("temp_max"); v = temp_at(i)
                if v is not None:
                    if tmin is not None and float(v) < float(tmin): flags.append("temp_low")
                    if tmax is not None and float(v) > float(tmax): flags.append("temp_high")
            if consider.get("humidity"):
                hmin = th.get("humidity_min"); hmax = th.get("humidity_max"); v = rh_at(i)
                if v is not None:
                    if hmin is not None and float(v) < float(hmin): flags.append("humidity_low")
                    if hmax is not None and float(v) > float(hmax): flags.append("humidity_high")
            if consider.get("wind"):
                wmin = th.get("wind_min"); wmax = th.get("wind_max"); v = wind_at(i)
                if v is not None:
                    if wmin is not None and float(v) < float(wmin): flags.append("wind_low")
                    if wmax is not None and float(v) > float(wmax): flags.append("wind_high")
            ext_flags.append(flags)

        factor_keys = [c["key"] for c in cond_summary]
        unideal_spans: Dict[str, List[Dict[str, Any]]] = {}
        for key in factor_keys:
            mask = [any(map_factor(f) == key for f in fl) for fl in ext_flags]
            unideal_spans[key] = group_ranges(ext_times, mask)

        # ---- alt windows (same length as selection) ----
        sel_len_hours = max(1, int(round((end_dt - start_dt).total_seconds() / 3600)))
        ok_mask_ext = [len(fl) == 0 for fl in ext_flags]

        def find_ok_block(start_index: int, forward: bool) -> Dict[str, Any] | None:
            needed = sel_len_hours
            n = len(ext_times)
            if forward:
                i = start_index
                while i + needed <= n:
                    if all(ok_mask_ext[i:i+needed]):
                        return {"start": ext_times[i].isoformat(),
                                "end": ext_times[i+needed-1].isoformat()}
                    i += 1
            else:
                i = start_index
                while i - needed + 1 >= 0:
                    if all(ok_mask_ext[i-needed+1:i+1]):
                        return {"start": ext_times[i-needed+1].isoformat(),
                                "end": ext_times[i].isoformat()}
                    i -= 1
            return None

        alt_windows: List[Dict[str, Any]] = []
        before_idx = None
        after_idx = None
        for j, t in enumerate(ext_times):
            if t < sel_times[0]:
                before_idx = j
            if after_idx is None and t > sel_times[-1]:
                after_idx = j
        if before_idx is not None:
            block = find_ok_block(before_idx, forward=False)
            if block: block["direction"] = "before"; alt_windows.append(block)
        if after_idx is not None:
            block = find_ok_block(after_idx, forward=True)
            if block: block["direction"] = "after"; alt_windows.append(block)

        # save trip summary
        trips = load_trips()
        trip_id = f"t{int(datetime.now().timestamp())}"
        trips.append({
            "id": trip_id,
            "name": trip_name,
            "coords": {"lat": float(lat), "lon": float(lon)},
            "timezone": tz,
            "window": {"start_local": start_dt.isoformat(), "end_local": end_dt.isoformat()},
            "prefs": prefs,
            "last_result": {"meets": meets, "violations": violations}
        })
        save_trips(trips)

        summary = "✅ All selected hours meet your conditions." if meets else "❌ Some hours violate your conditions."
        return {
            "ok": True,
            "timezone": tz,
            "meets": meets,
            "violations": violations,   # selected-window grouped ranges (any factor)
            "summary": summary,
            "hourly": hourly_eval,      # per-hour within selected window
            "conditions": cond_summary, # observed min/max
            "unideal_spans": unideal_spans,  # per-factor spans (full date+time)
            "alt_windows": alt_windows       # suggested windows of same duration
        }

    except Exception as e:
        print("[plan_trip error]", e)
        traceback.print_exc()
        return {"ok": False, "error": f"plan_trip crashed: {e}"}, 200


# ---------- trips CRUD ----------
@app.get("/api/trips", endpoint="trips_list")
def list_trips():
    try:
        return {"ok": True, "trips": load_trips()}
    except Exception as e:
        return {"ok": False, "error": f"list_trips failed: {e}"}, 200

@app.post("/api/trips/delete", endpoint="trips_delete")
def delete_trip():
    try:
        body = request.get_json(force=True) or {}
        trip_id = body.get("id")
        if not trip_id:
            return {"ok": False, "error": "id required"}, 400
        trips = [t for t in load_trips() if t.get("id") != trip_id]
        save_trips(trips)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": f"delete_trips failed: {e}"}, 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"[TripPlanner] http://127.0.0.1:{port}")
    app.run(host="0.0.0.0", port=port, debug=True)
