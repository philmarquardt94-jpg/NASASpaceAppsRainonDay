import os
import io
import csv
import json
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests
from flask import Flask, jsonify, request, render_template, Response
from zoneinfo import ZoneInfo

# ----------------------- Flask -----------------------
app = Flask(__name__, template_folder="templates", static_folder="static")

ROOT = Path(__file__).parent.resolve()
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
TRIPS_IDX = DATA_DIR / "trips_index.json"

# ----------------------- helpers -----------------------
def clamp(v, lo, hi): return max(lo, min(hi, v))

def load_idx() -> Dict:
    if TRIPS_IDX.exists():
        try:
            return json.loads(TRIPS_IDX.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"trips": []}

def save_idx(idx: Dict):
    TRIPS_IDX.write_text(json.dumps(idx, ensure_ascii=False, indent=2), encoding="utf-8")

def fahr_to_c(x): return (x - 32.0) * 5.0/9.0
def c_to_f(x):     return (x * 9.0/5.0) + 32.0
def ms_to_mph(x):  return x * 2.2369362921
def mph_to_ms(x):  return x / 2.2369362921
def mm_to_in(x):   return x / 25.4
def in_to_mm(x):   return x * 25.4

def round_sig(x, n=4):
    try:
        return float(f"{float(x):.{n}g}")
    except Exception:
        return x

def within(v, lo, hi):
    ok = True
    if lo is not None and v is not None and v < lo: ok = False
    if hi is not None and v is not None and v > hi: ok = False
    return ok

def nearest_boundary_distance(val: Optional[float], lo: Optional[float], hi: Optional[float]) -> Optional[float]:
    if val is None: return None
    d = []
    if lo is not None: d.append(abs(val - lo))
    if hi is not None: d.append(abs(val - hi))
    if not d: return None
    return min(d)

def flip_prob_label(p: Optional[float]) -> str:
    if p is None: return "green"
    if p >= 0.7: return "red"
    if p >= 0.3: return "yellow"
    return "green"

# ----------------------- data sources -----------------------
def fetch_open_meteo(lat: float, lon: float) -> Dict:
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        "&hourly=temperature_2m,relative_humidity_2m,precipitation_probability,precipitation,wind_speed_10m"
        "&forecast_days=16&timezone=auto"
    )
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    return r.json()

def fetch_nasa_power(lat: float, lon: float) -> Dict:
    # POWER hourly values (re)analysis proxy
    url = (
        "https://power.larc.nasa.gov/api/temporal/hourly/point"
        f"?latitude={lat}&longitude={lon}"
        "&parameters=T2M,RH2M,PRECTOTCORR,WS10M"
        "&community=RE&format=JSON&start=20250101&end=20260101"
    )
    r = requests.get(url, timeout=45)
    r.raise_for_status()
    return r.json()

def extract_nasa_series(power: Dict) -> Dict[str, List]:
    """
    Parse POWER hourly -> arrays. We tolerate missing keys and return empty arrays gracefully.
    Expected nested structure: properties.parameter.<PARAM>.<YYYYMMDD>.<HH>
    """
    try:
        props = power.get("properties", {})
        params = props.get("parameter", {})
        if not params:
            return {"time": [], "temp_C": [], "humidity_pct": [], "precip_mm": [], "wind_ms": []}
        # use whatever param to iterate dates/hours
        any_param = next(iter(params.values()))
        days = sorted(any_param.keys())
        times = []
        t2m = []; rh = []; pre = []; ws = []
        for day in days:
            hours = sorted(any_param[day].keys(), key=lambda h: int(h))
            for hh in hours:
                iso = f"{day[:4]}-{day[4:6]}-{day[6:]}T{int(hh):02d}:00:00Z"
                times.append(iso)
                t2m.append(_to_float(params.get("T2M", {}).get(day, {}).get(hh)))
                rh.append(_to_float(params.get("RH2M", {}).get(day, {}).get(hh)))
                pre.append(_to_float(params.get("PRECTOTCORR", {}).get(day, {}).get(hh)))
                ws.append(_to_float(params.get("WS10M", {}).get(day, {}).get(hh)))
        return {"time": times, "temp_C": t2m, "humidity_pct": rh, "precip_mm": pre, "wind_ms": ws}
    except Exception:
        return {"time": [], "temp_C": [], "humidity_pct": [], "precip_mm": [], "wind_ms": []}

def _to_float(x):
    try:
        return None if x is None or x == "" else float(x)
    except Exception:
        return None

# ---- NASA POWER climatology (monthly/hourly) ----
def fetch_power_climo(lat: float, lon: float) -> Dict:
    """
    Ask POWER 'temporal/climatology/point' for month/hour normals.
    We request the same core variables we've been using. If the service
    is unavailable or shape is different, we fail soft.
    """
    url = (
        "https://power.larc.nasa.gov/api/temporal/climatology/point"
        f"?latitude={lat}&longitude={lon}"
        "&parameters=T2M,RH2M,PRECTOTCORR,WS10M"
        "&community=RE&format=JSON"
    )
    r = requests.get(url, timeout=45)
    r.raise_for_status()
    return r.json()

def extract_climo_month_hour(power_climo: Dict, month_num: int) -> Dict[str, Dict[int, float]]:
    """
    Return { PARAM: { hour(0-23): value } } for a single month.
    POWER climatology varies by API version; we try a few common layouts:
      properties.parameter.<PARAM>.<month_key>.<hour_key>
    where month_key might be '1'..'12' or '01'..'12' or 'JAN'..'DEC'
    and hour_key is '0'..'23'.
    """
    out: Dict[str, Dict[int, float]] = {}
    try:
        props = power_climo.get("properties", {})
        params = props.get("parameter", {})
        if not params:
            return out

        # Candidate month keys
        m = month_num
        month_keys = {str(m), f"{m:02d}", _month_name(m), _month_name(m, short=True)}
        for param_name, months in params.items():
            # find a month key present
            chosen_month = None
            for mk in month_keys:
                if mk in months:
                    chosen_month = mk
                    break
            if not chosen_month:
                # Some responses flatten month dimension or use ALL-month mean; try direct hour map
                if isinstance(months, dict) and all(k.isdigit() for k in months.keys()):
                    try:
                        hours_map = {int(h): _to_float(v) for h, v in months.items()}
                        out[param_name] = hours_map
                    except Exception:
                        pass
                continue

            hours = months.get(chosen_month, {})
            hours_map: Dict[int, float] = {}
            for hh, val in hours.items():
                try:
                    hours_map[int(hh)] = _to_float(val)
                except Exception:
                    continue
            out[param_name] = hours_map
    except Exception:
        pass
    return out

def _month_name(i: int, short: bool=False) -> str:
    names = [
        ("JANUARY","JAN"),("FEBRUARY","FEB"),("MARCH","MAR"),("APRIL","APR"),
        ("MAY","MAY"),("JUNE","JUN"),("JULY","JUL"),("AUGUST","AUG"),
        ("SEPTEMBER","SEP"),("OCTOBER","OCT"),("NOVEMBER","NOV"),("DECEMBER","DEC")
    ]
    full, sh = names[i-1]
    return sh if short else full

# ----------------------- evaluation -----------------------
def evaluate_hour(values: Dict, th: Dict) -> Tuple[bool, List[str], Dict[str, bool]]:
    reasons = []
    per_ok = {}

    if th.get("precip_prob_max") is not None:
        ok = values["precip_prob"] is None or values["precip_prob"] <= th["precip_prob_max"]
        per_ok["precip_prob"] = ok
        if not ok: reasons.append("precip_prob")

    if th.get("precip_amt_max") is not None:
        ok = values["precip_mm"] is None or values["precip_mm"] <= th["precip_amt_max"]
        per_ok["precip_amt"] = ok
        if not ok: reasons.append("precip_amt")

    if th.get("temp_min") is not None or th.get("temp_max") is not None:
        ok = within(values["temp_C"], th.get("temp_min"), th.get("temp_max"))
        per_ok["temp"] = ok
        if not ok: reasons.append("temp")

    if th.get("humidity_min") is not None or th.get("humidity_max") is not None:
        ok = within(values["humidity_pct"], th.get("humidity_min"), th.get("humidity_max"))
        per_ok["humidity"] = ok
        if not ok: reasons.append("humidity")

    if th.get("wind_min") is not None or th.get("wind_max") is not None:
        ok = within(values["wind_ms"], th.get("wind_min"), th.get("wind_max"))
        per_ok["wind"] = ok
        if not ok: reasons.append("wind")

    overall_ok = len(reasons) == 0
    reason_map = {
        "precip_prob": "precip_high",
        "precip_amt": "precip_amt_high",
        "temp": "temp_out_of_range",
        "humidity": "humidity_out_of_range",
        "wind": "wind_high",
    }
    reasons = [reason_map[r] for r in reasons]
    return overall_ok, reasons, per_ok

def compute_flip_from_models(
    om_val: Optional[float],
    nasa_val: Optional[float],
    lo: Optional[float],
    hi: Optional[float],
    *, no_thresh_scale: float = 0.25
) -> Optional[float]:
    if om_val is None or nasa_val is None:
        return None
    diff = abs(nasa_val - om_val)
    dist = nearest_boundary_distance(om_val, lo, hi)
    if dist is not None and dist > 0:
        p = diff / (dist + 1e-6)
        return clamp(p, 0.0, 1.0)
    p = diff * no_thresh_scale
    return clamp(p, 0.0, 1.0)

# ----------------------- reverse geocode & tz -----------------------
@app.post("/api/reverse_geocode")
def reverse_geocode():
    data = request.get_json(force=True)
    lat, lon = data.get("lat"), data.get("lon")
    try:
        r = requests.get(
            "https://nominatim.openstreetmap.org/reverse",
            params={"format": "jsonv2", "lat": lat, "lon": lon},
            headers={"User-Agent": "trip-planner/1.0"},
            timeout=20,
        )
        r.raise_for_status()
        j = r.json()
        name = j.get("display_name") or "Unknown"
        return jsonify(ok=True, name=name, display_name=name)
    except Exception as e:
        return jsonify(ok=False, error=f"{e}")

@app.get("/api/hourly")
def api_hourly():
    lat = float(request.args.get("lat"))
    lon = float(request.args.get("lon"))
    j = fetch_open_meteo(lat, lon)
    return jsonify(ok=True, data={"timezone": j.get("timezone") or "UTC"})

# ----------------------- planning -----------------------
@app.post("/api/plan_trip")
def plan_trip():
    body = request.get_json(force=True)
    trip_name = body.get("trip_name") or "Untitled Trip"
    lat = float(body["lat"]); lon = float(body["lon"])
    window = body["window"]
    data_source = (body.get("data_source") or "open-meteo").lower()

    prefs = body.get("prefs") or {}
    consider = prefs.get("consider") or {}
    units = prefs.get("units") or {}
    th_user = prefs.get("thresholds") or {}

    # Canonical thresholds (C, %, m/s, mm)
    th = {
        "precip_prob_max": th_user.get("precip_prob_max"),
        "precip_amt_max":  None if th_user.get("precip_amt_max") is None
                           else (th_user["precip_amt_max"] if (units.get("precip_amt") == "mm") else in_to_mm(th_user["precip_amt_max"])),
        "temp_min": None if th_user.get("temp_min") is None
                    else (th_user["temp_min"] if units.get("temp") == "C" else fahr_to_c(th_user["temp_min"])),
        "temp_max": None if th_user.get("temp_max") is None
                    else (th_user["temp_max"] if units.get("temp") == "C" else fahr_to_c(th_user["temp_max"])),
        "humidity_min": th_user.get("humidity_min"),
        "humidity_max": th_user.get("humidity_max"),
        "wind_min": None if th_user.get("wind_min") is None
                    else (th_user["wind_min"] if units.get("wind") == "m/s" else mph_to_ms(th_user["wind_min"])),
        "wind_max": None if th_user.get("wind_max") is None
                    else (th_user["wind_max"] if units.get("wind") == "m/s" else mph_to_ms(th_user["wind_max"])),
    }

    # ---- fetch Open-Meteo ----
    om = fetch_open_meteo(lat, lon)
    tz_name = om.get("timezone") or "UTC"
    local_tz = ZoneInfo(tz_name)
    om_time = [t + ":00" if len(t) == 16 else t for t in om.get("hourly", {}).get("time", [])]
    om_temp_c = om.get("hourly", {}).get("temperature_2m", [])
    om_hum = om.get("hourly", {}).get("relative_humidity_2m", [])
    om_precip_prc = om.get("hourly", {}).get("precipitation_probability", [])
    om_precip_mm = om.get("hourly", {}).get("precipitation", [])
    om_wind_ms = om.get("hourly", {}).get("wind_speed_10m", [])

    # ---- fetch NASA POWER hourly (for uncertainty) ----
    try:
        power = fetch_nasa_power(lat, lon)
        nasa_series = extract_nasa_series(power)
    except Exception:
        nasa_series = {"time": [], "temp_C": [], "humidity_pct": [], "precip_mm": [], "wind_ms": []}

    # Choose main series (Combined uses OM values for user-facing series)
    def choose_series(which: str):
        if which == "nasa-power" or which == "nasa":
            return nasa_series.get("time", []), {
                "temp_C":     nasa_series.get("temp_C", []),
                "humidity":   nasa_series.get("humidity_pct", []),
                "precip_mm":  nasa_series.get("precip_mm", []),
                "precip_prc": [None]*len(nasa_series.get("time", [])),
                "wind_ms":    nasa_series.get("wind_ms", []),
            }
        # default / combined -> OM
        return om_time, {
            "temp_C":     om_temp_c,
            "humidity":   om_hum,
            "precip_mm":  om_precip_mm,
            "precip_prc": om_precip_prc,
            "wind_ms":    om_wind_ms,
        }

    values_times, vals_all = choose_series(data_source if data_source != "combined" else "open-meteo")
    if not values_times:
        return jsonify(ok=True, no_data_for_window=True, message="No data available for the selected source/time.")

    s_local = datetime.fromisoformat(window["start_local"])
    e_local = datetime.fromisoformat(window["end_local"])

    # Slice window (convert POWER 'Z' timestamps to local)
    idx = []
    for i, t in enumerate(values_times):
        if t.endswith("Z"):
            dt_utc = datetime.fromisoformat(t.replace("Z", "+00:00"))
            dt_local = dt_utc.astimezone(local_tz).replace(tzinfo=None)
        else:
            dt_local = datetime.fromisoformat(t)
        if s_local <= dt_local < e_local:
            idx.append(i)

    if not idx:
        return jsonify(ok=True, no_data_for_window=True, message="No hourly data inside the provided window.")

    def slice_arr(a): return [a[i] if i < len(a) else None for i in idx]
    t_sel     = slice_arr(values_times)
    temp_c    = slice_arr(vals_all["temp_C"])
    hum_pct   = slice_arr(vals_all["humidity"])
    precip_mm = slice_arr(vals_all["precip_mm"])
    precip_prc= slice_arr(vals_all["precip_prc"])
    wind_ms   = slice_arr(vals_all["wind_ms"])

    # Align POWER hourly by exact ISO (try with/without Z)
    nasa_map = { (nasa_series.get("time") or [])[i]: i for i in range(len(nasa_series.get("time") or [])) }
    def nasa_val_at(iso, key):
        j = nasa_map.get(iso)
        if j is None:
            zver = iso if iso.endswith("Z") else iso + "Z"
            j = nasa_map.get(zver)
        if j is None: return None
        arr = nasa_series.get(key) or []
        return arr[j] if j < len(arr) else None

    # Hourly evaluation and model-disagreement heuristic
    hourly = []
    hourly_flip: List[Dict[str, Optional[float]]] = []
    hourly_factor_ok: List[Dict[str, Optional[bool]]] = []

    for i in range(len(t_sel)):
        values = {
            "temp_C": temp_c[i],
            "humidity_pct": hum_pct[i],
            "wind_ms": wind_ms[i],
            "precip_mm": precip_mm[i],
            "precip_prob": precip_prc[i],
        }
        ok, reasons, per_ok = evaluate_hour(values, th)
        hourly.append({"time": t_sel[i], "ok": ok, "reasons": reasons})
        hourly_factor_ok.append(per_ok)

        flip_map = {
            "temp":       compute_flip_from_models(values["temp_C"],     nasa_val_at(t_sel[i], "temp_C"),     th.get("temp_min"), th.get("temp_max")),
            "humidity":   compute_flip_from_models(values["humidity_pct"],nasa_val_at(t_sel[i], "humidity_pct"), th.get("humidity_min"), th.get("humidity_max"), no_thresh_scale=0.01),
            "wind":       compute_flip_from_models(values["wind_ms"],    nasa_val_at(t_sel[i], "wind_ms"),    th.get("wind_min"), th.get("wind_max")),
            "precip_amt": compute_flip_from_models(values["precip_mm"],  nasa_val_at(t_sel[i], "precip_mm"),  None, th.get("precip_amt_max")),
            "precip_prob": None,  # no POWER probability
        }
        hourly_flip.append(flip_map)

    # Observed min/max in window
    def obs_minmax(arr):
        nums = [x for x in arr if x is not None]
        return (min(nums) if nums else None, max(nums) if nums else None)

    temp_min_c, temp_max_c = obs_minmax(temp_c)
    hum_min, hum_max = obs_minmax(hum_pct)
    wind_min_ms, wind_max_ms = obs_minmax(wind_ms)
    pamt_min_mm, pamt_max_mm = obs_minmax(precip_mm)
    pprob_min, pprob_max = obs_minmax(precip_prc)

    # Presentational units
    unit_temp = units.get("temp", "C")
    unit_wind = units.get("wind", "m/s")
    unit_pamt = units.get("precip_amt", "mm")
    def to_ui_temp(x):  return None if x is None else round_sig(c_to_f(x) if unit_temp=="F" else x)
    def to_ui_wind(x):  return None if x is None else round_sig(ms_to_mph(x) if unit_wind=="mph" else x)
    def to_ui_pamt(x):  return None if x is None else round_sig(mm_to_in(x) if unit_pamt=="in" else x)

    # Per-factor 'all OK' flags for coloring
    def factor_all_ok(key: str) -> Optional[bool]:
        any_marked = False
        for m in hourly_factor_ok:
            if key in m:
                any_marked = True
                if not m[key]:
                    return False
        return True if any_marked else None

    def avg_flip(key: str) -> Optional[float]:
        vals = [fm.get(key) for fm in hourly_flip if fm.get(key) is not None]
        if not vals: return None
        return sum(vals)/len(vals)

    conds = []
    def add_cond(key, label, min_val, max_val, unit):
        ok_factor = factor_all_ok(key)
        avg = avg_flip(key)
        conds.append({
            "key": key,
            "label": label,
            "min": min_val,
            "max": max_val,
            "unit": unit,
            "ok": ok_factor if ok_factor is not None else True,
            "flip_prob": avg,
            "flip_label": flip_prob_label(avg)
        })

    add_cond("precip_prob", "Precip. Probability", pprob_min, pprob_max, "%")
    add_cond("precip_amt",  "Precip. Amount",      to_ui_pamt(pamt_min_mm), to_ui_pamt(pamt_max_mm), unit_pamt)
    add_cond("temp",        "Temperature",         to_ui_temp(temp_min_c),  to_ui_temp(temp_max_c),  unit_temp)
    add_cond("humidity",    "Humidity",            hum_min,  hum_max, "%")
    add_cond("wind",        "Wind Speed",          to_ui_wind(wind_min_ms), to_ui_wind(wind_max_ms), unit_wind)

    # Violations spans
    violations = []
    current_span = None
    for h in hourly:
        if not h["ok"]:
            if current_span is None:
                current_span = {"start": h["time"], "end": h["time"]}
            else:
                current_span["end"] = h["time"]
        else:
            if current_span:
                end_dt = datetime.fromisoformat(current_span["end"]) + timedelta(hours=1)
                current_span["end"] = end_dt.isoformat()
                violations.append(current_span)
                current_span = None
    if current_span:
        end_dt = datetime.fromisoformat(current_span["end"]) + timedelta(hours=1)
        current_span["end"] = end_dt.isoformat()
        violations.append(current_span)

    # Unideal spans per factor
    def spans_for_factor(key: str) -> List[Dict]:
        res = []; span=None
        for i, h in enumerate(hourly):
            ok_map = hourly_factor_ok[i]
            if key not in ok_map: continue
            if not ok_map[key]:
                if span is None: span={"start": h["time"], "end": h["time"]}
                else: span["end"]=h["time"]
            else:
                if span:
                    end_dt = datetime.fromisoformat(span["end"]) + timedelta(hours=1)
                    span["end"]=end_dt.isoformat(); res.append(span); span=None
        if span:
            end_dt = datetime.fromisoformat(span["end"]) + timedelta(hours=1)
            span["end"]=end_dt.isoformat(); res.append(span)
        return res

    unideal_spans = {
        "precip_prob": spans_for_factor("precip_prob") if consider.get("precip_prob") else [],
        "precip_amt":  spans_for_factor("precip_amt") if consider.get("precip_amt") else [],
        "temp":        spans_for_factor("temp") if consider.get("temp") else [],
        "humidity":    spans_for_factor("humidity") if consider.get("humidity") else [],
        "wind":        spans_for_factor("wind") if consider.get("wind") else [],
    }

    # Alt windows (same duration)
    duration = e_local - s_local
    alt_windows = [
        {"direction": "before", "start": (s_local - duration).isoformat(), "end": (e_local - duration).isoformat()},
        {"direction": "after",  "start": (s_local + duration).isoformat(), "end": (e_local + duration).isoformat()},
    ]

    meets = all(h["ok"] for h in hourly)
    summary = "All selected hours meet your conditions." if meets else "Some hours violate your conditions."

    # UI series (units converted)
    series_ui = {
        "time": t_sel,
        "temp": [to_ui_temp(x) for x in temp_c],
        "humidity": hum_pct,
        "wind": [to_ui_wind(x) for x in wind_ms],
        "precip_amt": [to_ui_pamt(x) for x in precip_mm],
        "precip_prob": precip_prc
    }

    # --------- Climatology Assist (vs normal) ---------
    climatology = {}
    try:
        climo_raw = fetch_power_climo(lat, lon)
        month_num = datetime.now(ZoneInfo("UTC")).astimezone(local_tz).month
        climo_map = extract_climo_month_hour(climo_raw, month_num)
        # Build mean for the hours in window for each factor
        def hours_local_list(iso_list):
            out_hours = []
            for t in iso_list:
                if t.endswith("Z"):
                    dt_local = datetime.fromisoformat(t.replace("Z", "+00:00")).astimezone(local_tz)
                else:
                    dt_local = datetime.fromisoformat(t).replace(tzinfo=local_tz)
                out_hours.append(dt_local.hour)
            return out_hours

        hrs = hours_local_list(t_sel)

        def avg_climo(param):
            m = climo_map.get(param) or {}
            vals = [m.get(h) for h in hrs if m.get(h) is not None]
            return (sum(vals)/len(vals)) if vals else None

        def avg_obs(values):
            nums = [x for x in values if x is not None]
            return (sum(nums)/len(nums)) if nums else None

        # Temperature
        c_mean = avg_climo("T2M")
        o_mean = avg_obs([to_ui_temp(x) for x in temp_c]) if unit_temp == "C" else avg_obs([c_to_f(x) if x is not None else None for x in temp_c])
        climatology["temp"] = _climo_obj(c_mean, o_mean, unit_temp, "cooler", "near normal", "warmer")

        # Humidity
        c_mean = avg_climo("RH2M")
        o_mean = avg_obs(hum_pct)
        climatology["humidity"] = _climo_obj(c_mean, o_mean, "%", "drier", "near normal", "more humid")

        # Wind
        c_mean_ms = avg_climo("WS10M")
        # express in UI units
        if c_mean_ms is not None:
            c_mean_ui = ms_to_mph(c_mean_ms) if unit_wind == "mph" else c_mean_ms
        else:
            c_mean_ui = None
        o_mean_ui = avg_obs(series_ui["wind"])
        climatology["wind"] = _climo_obj(c_mean_ui, o_mean_ui, unit_wind, "calmer", "near normal", "windier")

        # Precip amount
        c_mean_mm = avg_climo("PRECTOTCORR")
        c_mean_ui = mm_to_in(c_mean_mm) if (c_mean_mm is not None and unit_pamt == "in") else c_mean_mm
        o_mean_ui = avg_obs(series_ui["precip_amt"])
        climatology["precip_amt"] = _climo_obj(c_mean_ui, o_mean_ui, unit_pamt, "lower", "near normal", "higher")
    except Exception:
        climatology = {}

    # NASA Uncertainty (averaged)
    unc = [
        {"key":"precip_prob","label":"Precip. Probability","flip_prob": avg_flip("precip_prob"),"flip_label": flip_prob_label(avg_flip("precip_prob"))},
        {"key":"Temperature","label":"Temperature","flip_prob": avg_flip("temp"),"flip_label": flip_prob_label(avg_flip("temp"))},
        {"key":"Wind Speed","label":"Wind Speed","flip_prob": avg_flip("wind"),"flip_label": flip_prob_label(avg_flip("wind"))},
    ]

    trip_id = f"t{int(datetime.utcnow().timestamp())}{uuid.uuid4().hex[:6]}"
    result = {
        "ok": True,
        "trip_id": trip_id,
        "name": trip_name,
        "coords": {"lat": lat, "lon": lon},
        "timezone": tz_name,
        "data_source": data_source,
        "window": {"start_local": s_local.isoformat(), "end_local": e_local.isoformat()},
        "prefs": {"consider": consider, "units": units, "thresholds": th_user},
        "series": series_ui,
        "hourly": hourly,
        "hourly_flip": hourly_flip,
        "hourly_factor_ok": hourly_factor_ok,
        "conditions": conds,
        "uncertainty": unc,
        "climatology": climatology,   # <--- new
        "violations": violations,
        "unideal_spans": unideal_spans,
        "alt_windows": alt_windows,
        "meets": meets,
        "summary": summary,
    }

    # Persist full report
    (DATA_DIR / f"{trip_id}.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    # Update index
    idx_index = load_idx()
    idx_index["trips"].insert(0, {
        "id": trip_id,
        "name": trip_name,
        "timezone": tz_name,
        "coords": {"lat": lat, "lon": lon},
        "window": result["window"],
        "last_result": {"meets": meets}
    })
    save_idx(idx_index)

    return jsonify(result)

def _climo_obj(c_mean, o_mean, unit, low_lbl, mid_lbl, hi_lbl):
    """
    Compose the object the front-end expects:
      { mean, unit, pct_of_normal, label }
    """
    if c_mean is None or o_mean is None or c_mean == 0:
        return {"mean": c_mean, "unit": unit, "pct_of_normal": None, "label": None}
    pct = (o_mean / c_mean) * 100.0
    lbl = mid_lbl
    if pct <= 90: lbl = low_lbl
    elif pct >= 110: lbl = hi_lbl
    return {"mean": round_sig(c_mean), "unit": unit, "pct_of_normal": round_sig(pct), "label": lbl}

# ----------------------- trips API -----------------------
@app.get("/api/trips")
def list_trips():
    idx = load_idx()
    return jsonify(ok=True, trips=idx.get("trips", []))

@app.get("/api/trip/<trip_id>")
def get_trip(trip_id: str):
    p = DATA_DIR / f"{trip_id}.json"
    if not p.exists():
        return jsonify(ok=False, error="Not found"), 404
    j = json.loads(p.read_text(encoding="utf-8"))
    return jsonify(ok=True, trip=j)

@app.post("/api/trips/delete")
def delete_trip():
    data = request.get_json(force=True)
    trip_id = data.get("id")
    idx = load_idx()
    idx["trips"] = [t for t in idx.get("trips", []) if t.get("id") != trip_id]
    save_idx(idx)
    p = DATA_DIR / f"{trip_id}.json"
    if p.exists():
        try: p.unlink()
        except Exception: pass
    return jsonify(ok=True)

# ----------------------- CSV export -----------------------
@app.get("/api/trip/<trip_id>/csv", endpoint="trip_csv")
def trip_csv(trip_id: str):
    try:
        p = DATA_DIR / f"{trip_id}.json"
        if not p.exists():
            return Response("Trip not found", status=404)
        data = json.loads(p.read_text(encoding="utf-8"))

        factor = (request.args.get("factor") or "all").lower()
        with_summary = request.args.get("with_summary") in ("1","true","yes")

        tz = data.get("timezone") or "UTC"
        prefs = data.get("prefs") or {}
        th = prefs.get("thresholds") or {}
        units = prefs.get("units") or {}

        label_map = {
            "temp": "Temperature",
            "humidity": "Humidity",
            "wind": "Wind Speed",
            "precip_amt": "Precip. Amount",
            "precip_prob": "Precip. Probability",
        }
        unit_map = {
            "temp": units.get("temp") or "C",
            "humidity": "%",
            "wind": units.get("wind") or "m/s",
            "precip_amt": units.get("precip_amt") or "mm",
            "precip_prob": "%",
        }

        series = data.get("series") or {}
        times = series.get("time") or []
        hourly = data.get("hourly") or []
        hourly_flip = data.get("hourly_flip") or []
        hourly_factor_ok = data.get("hourly_factor_ok") or []
        conds = {c["key"]: c for c in (data.get("conditions") or [])}

        def flip_pct_base(k):
            c = conds.get(k)
            if not c: return ""
            fp = c.get("flip_prob")
            return "" if fp is None else f"{int(round(fp*100))}%"

        out = io.StringIO()
        w = csv.writer(out)

        if with_summary:
            w.writerow([f"# Trip: {data.get('name','')} ({trip_id})"])
            w.writerow([f"# Window: {data.get('window',{}).get('start_local','')} -> {data.get('window',{}).get('end_local','')} ({tz})"])
            for k in ("temp","humidity","wind","precip_amt","precip_prob"):
                c = conds.get(k)
                if not c: continue
                w.writerow([f"# {label_map.get(k,k)} min={c.get('min')} {unit_map.get(k,'')}, max={c.get('max')} {unit_map.get(k,'')}, flip_prob={flip_pct_base(k)}"])
            w.writerow([f"# Thresholds used (user input):"])
            if th.get("temp_min") is not None or th.get("temp_max") is not None:
                w.writerow([f"#  Temperature: min={th.get('temp_min')} {unit_map['temp']}, max={th.get('temp_max')} {unit_map['temp']}"])
            if th.get("humidity_min") is not None or th.get("humidity_max") is not None:
                w.writerow([f"#  Humidity: min={th.get('humidity_min')} %, max={th.get('humidity_max')} %"])
            if th.get("wind_min") is not None or th.get("wind_max") is not None:
                w.writerow([f"#  Wind: min={th.get('wind_min')} {unit_map['wind']}, max={th.get('wind_max')} {unit_map['wind']}"])
            if th.get("precip_amt_max") is not None:
                w.writerow([f"#  Precip. Amount: max={th.get('precip_amt_max')} {unit_map['precip_amt']}"])
            if th.get("precip_prob_max") is not None:
                w.writerow([f"#  Precip. Probability: max={th.get('precip_prob_max')} %"])
            w.writerow([])

        keys_all = ["precip_prob", "precip_amt", "temp", "wind", "humidity"]
        keys = keys_all if factor == "all" else [factor]

        headers = ["time"]
        for k in keys:
            headers.append(f"{label_map.get(k,k)} ({unit_map.get(k,'')})")
            headers.append(f"{label_map.get(k,k)} Ideal (Yes/No)")
            headers.append(f"{label_map.get(k,k)} Chance to Flip (per-hour)")
        headers += ["Overall OK", "Reasons"]
        w.writerow(headers)

        def series_value(k, i):
            if k == "temp":         return (series.get("temp") or [None]*len(times))[i]
            if k == "humidity":     return (series.get("humidity") or [None]*len(times))[i]
            if k == "wind":         return (series.get("wind") or [None]*len(times))[i]
            if k == "precip_amt":   return (series.get("precip_amt") or [None]*len(times))[i]
            if k == "precip_prob":  return (series.get("precip_prob") or [None]*len(times))[i]
            return None

        for i, t in enumerate(times):
            row = [t]
            for k in keys:
                v = series_value(k, i)
                row.append("" if v is None else v)

                ok_map = hourly_factor_ok[i] if i < len(hourly_factor_ok) else {}
                ideal = ok_map.get(k)
                row.append("" if ideal is None else ("Yes" if ideal else "No"))

                flips = hourly_flip[i] if i < len(hourly_flip) else {}
                fp = flips.get(k)
                row.append("" if fp is None else f"{int(round(fp*100))}%")

            h = hourly[i] if i < len(hourly) else {}
            row.append("Yes" if h.get("ok") else "No")
            row.append(";".join(h.get("reasons") or []))
            w.writerow(row)

        csv_bytes = out.getvalue().encode("utf-8")
        fname = f"{trip_id}_{factor}.csv"
        return Response(
            csv_bytes,
            mimetype="text/csv",
            headers={"Content-Disposition": f"attachment; filename={fname}"}
        )
    except Exception as e:
        return Response(f"trip_csv failed: {e}", status=500)

# ----------------------- index page -----------------------
@app.get("/")
def index():
    return render_template("index.html")

# ----------------------- run -----------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
