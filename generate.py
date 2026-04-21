#!/usr/bin/env python3
"""Garmin running analysis static site generator."""

import re
import sqlite3
import bisect
import json
import urllib.request
import urllib.error
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from jinja2 import Environment, FileSystemLoader
from markupsafe import Markup
from config import DB_PATH, OLLAMA_URL, OLLAMA_MODEL

STATIC_DIR     = Path(__file__).parent / "static"
AI_CACHE_PATH             = Path(__file__).parent / "ai-analysis-cache.json"
AI_RACE_CACHE_PATH        = Path(__file__).parent / "ai-race-analysis-cache.json"
AI_CALORIE_CACHE_PATH     = Path(__file__).parent / "ai-calorie-cache.json"
AI_CALORIE_STRATA_CACHE_PATH = Path(__file__).parent / "ai-calorie-strata-cache.json"
ACTIVITIES_MANIFEST_PATH  = Path(__file__).parent / "dist" / "activities-manifest.json"
BEST_EFFORTS_CACHE_PATH   = Path(__file__).parent / "best-efforts-cache.json"
ACTIVITIES_MANIFEST_VERSION = "layout-v7"  # bump when activity.html template changes

# Calorie strata: (lower_bound_inclusive, label_description)
# Each stratum gets one healthy + one unhealthy AI-generated food equivalent.
CALORIE_STRATA = [
    (50,   "around 100 calories"),
    (150,  "around 200 calories"),
    (250,  "around 300 calories"),
    (350,  "around 400 calories"),
    (450,  "around 525 calories"),
    (600,  "around 675 calories"),
    (750,  "around 850 calories"),
    (950,  "around 1050 calories"),
    (1150, "around 1250 calories"),
    (1350, "around 1500 calories"),
    (1650, "around 1800 calories"),
    (2000, "around 2200 calories"),
    (2400, "about 2700 calories"),
]
CALORIE_STRATA_VERSION = "v1"

_ACT_LOG_FIELDS = (
    "activity_id", "activity_name", "activity_type_key", "date",
    "miles", "duration_fmt", "pace_mile", "average_hr", "is_race", "elev_gain_fmt",
)
TEMPLATES_DIR = Path(__file__).parent / "templates"
DIST_DIR = Path(__file__).parent / "dist"

RUNNING_TYPES = (
    "running", "treadmill_running", "track_running",
    "trail_running", "road_running", "ultra_run",
)

# Activities excluded from all analysis (bad GPS clock, corrupt duration, etc.)
EXCLUDED_ACTIVITY_IDS: set[int] = {
    855380261,  # bad recorded time — skews pace/milestone data
}

RACE_DISTANCE_LABELS = {
    (0, 5.5): "5K",
    (5.5, 8.0): "~10K",
    (8.0, 12.0): "10K",
    (12.0, 18.0): "~HM",
    (18.0, 23.0): "Half Marathon",
    (23.0, 35.0): "~Marathon",
    (35.0, 45.0): "Marathon",
    (45.0, 60.0): "50K",
    (60.0, 9999): "Ultra",
}

# Best-split target distances in metres, with display labels
SPLIT_TARGETS = [
    (1_000,     "1K"),
    (1_609.344, "1 mile"),
    (5_000,     "5K"),
    (10_000,    "10K"),
    (15_000,    "15K"),
    (16_093.44, "10 mile"),
    (21_097.5,  "Half Marathon"),
    (30_000,    "30K"),
    (35_000,    "35K"),
    (42_195,    "Marathon"),
    (45_000,    "45K"),
    (50_000,    "50K"),
]

# Colour palette – distinct, readable on dark background
PALETTE = [
    "#00d4ff", "#10b981", "#f59e0b", "#ef4444", "#8b5cf6",
    "#ec4899", "#06b6d4", "#84cc16", "#f97316", "#a78bfa",
    "#34d399", "#fbbf24", "#f87171", "#60a5fa", "#c084fc",
    "#4ade80", "#fb923c", "#38bdf8", "#e879f9", "#a3e635",
]

# Filter categories shown on the analysis page (label -> list of dist labels that qualify)
ANALYSIS_CATEGORIES = [
    ("Marathon",      ["Marathon", "~Marathon"]),
    ("Half Marathon", ["Half Marathon", "~HM"]),
    ("10K",           ["10K", "~10K"]),
    ("5K",            ["5K"]),
    ("All",           None),   # None = everything
]

MONTH_NAMES = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]


# ─────────────────────────────────────────────
# Formatting helpers
# ─────────────────────────────────────────────

def classify_distance(km: float) -> str:
    for (lo, hi), label in RACE_DISTANCE_LABELS.items():
        if lo <= km < hi:
            return label
    return f"{km:.1f}km"


def fmt_pace(speed_mps: float) -> str:
    if not speed_mps or speed_mps <= 0:
        return "-"
    pace_sec_km = 1000 / speed_mps
    return f"{int(pace_sec_km // 60)}:{int(pace_sec_km % 60):02d}"


def fmt_pace_mile(speed_mps: float) -> str:
    if not speed_mps or speed_mps <= 0:
        return "-"
    pace_sec = 1609.344 / speed_mps
    return f"{int(pace_sec // 60)}:{int(pace_sec % 60):02d}"


def fmt_pace_from_sec_per_mi(sec: float) -> str:
    """Format seconds-per-mile as MM:SS string."""
    s = int(sec)
    return f"{s // 60}:{s % 60:02d}"


def fmt_split_diff(delta_s: float) -> str:
    """Format a signed seconds delta as ±M:SS for pace-vs-average display."""
    sign = "+" if delta_s >= 0 else "-"
    abs_s = int(abs(delta_s))
    return f"{sign}{abs_s // 60}:{abs_s % 60:02d}"


def fmt_duration(seconds: float) -> str:
    if not seconds:
        return "-"
    t = int(seconds)
    h, m, s = t // 3600, (t % 3600) // 60, t % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fmt_dist_km(meters: float) -> str:
    return f"{meters / 1000:.2f}" if meters else "-"


def fmt_dist_miles(meters: float) -> str:
    return f"{meters / 1609.344:.2f}" if meters else "-"


def hr_zone_pct(total_dur, *zone_times) -> list[float]:
    if not total_dur:
        return [0.0] * len(zone_times)
    return [round((z or 0) / total_dur * 100, 1) for z in zone_times]


def pace_from_seconds_meters(elapsed_s: float, meters: float) -> tuple[str, str]:
    if elapsed_s <= 0 or meters <= 0:
        return "-", "-"
    speed = meters / elapsed_s
    return fmt_pace(speed), fmt_pace_mile(speed)


# ─────────────────────────────────────────────
# Shared timeseries loaders
# ─────────────────────────────────────────────

def _get_t0(cursor: sqlite3.Cursor, activity_id: int):
    """Return the datetime of the first distance record (activity start)."""
    cursor.execute(
        "SELECT timestamp FROM activity_ts_metric "
        "WHERE activity_id=? AND name='distance' ORDER BY timestamp LIMIT 1",
        (activity_id,),
    )
    row = cursor.fetchone()
    return datetime.strptime(row[0][:19], "%Y-%m-%d %H:%M:%S") if row else None


def _load_named_series(
    cursor: sqlite3.Cursor, activity_id: int, name: str, t0
) -> tuple[list[float], list[float]]:
    """Return (elapsed_sec[], values[]) for a named metric relative to t0."""
    cursor.execute(
        "SELECT timestamp, value FROM activity_ts_metric "
        "WHERE activity_id=? AND name=? ORDER BY timestamp",
        (activity_id, name),
    )
    rows = cursor.fetchall()
    times, vals = [], []
    for ts_str, val in rows:
        ts = datetime.strptime(ts_str[:19], "%Y-%m-%d %H:%M:%S")
        times.append((ts - t0).total_seconds())
        vals.append(float(val))
    return times, vals


def _interp(times: list[float], vals: list[float], t: float):
    """Linear interpolation of vals at elapsed time t. Returns None if out of range."""
    if not times or t < times[0] or t > times[-1]:
        return None
    idx = bisect.bisect_right(times, t)
    if idx == 0:
        return vals[0]
    if idx >= len(times):
        return vals[-1]
    t0_, t1_ = times[idx - 1], times[idx]
    v0,  v1  = vals[idx - 1],  vals[idx]
    frac = (t - t0_) / (t1_ - t0_) if t1_ > t0_ else 0.0
    return v0 + frac * (v1 - v0)


def load_distance_series(
    cursor: sqlite3.Cursor, activity_id: int
) -> tuple[list[float], list[float]]:
    """
    Load the distance timeseries and return (elapsed_seconds[], cumulative_metres[]).
    Uses actual timestamp differences so sparse older recordings work correctly.
    """
    cursor.execute(
        "SELECT timestamp, value FROM activity_ts_metric "
        "WHERE activity_id=? AND name='distance' ORDER BY timestamp",
        (activity_id,),
    )
    rows = cursor.fetchall()
    if not rows:
        return [], []

    t0 = None
    times: list[float] = []
    dists: list[float] = []
    for ts_str, dist in rows:
        ts = datetime.strptime(ts_str[:19], "%Y-%m-%d %H:%M:%S")
        if t0 is None:
            t0 = ts
        times.append((ts - t0).total_seconds())
        dists.append(dist)

    return times, dists


# ─────────────────────────────────────────────
# Best splits (sliding window over TS data)
# ─────────────────────────────────────────────

def best_splits_for_activity(
    cursor: sqlite3.Cursor, activity_id: int, race_dist_m: float
) -> list[dict]:
    times, dists = load_distance_series(cursor, activity_id)
    if not dists:
        return []

    n = len(dists)
    results = []

    for target_m, label in SPLIT_TARGETS:
        if race_dist_m < target_m * 0.98:
            continue

        best_s = None
        best_lo = 0
        best_right = 0
        left = 0
        for right in range(n):
            while left < right and dists[right] - dists[left] > target_m:
                left += 1
            lo = left - 1 if left > 0 else 0
            if dists[right] - dists[lo] >= target_m:
                elapsed = times[right] - times[lo]   # actual seconds, not index delta
                if best_s is None or elapsed < best_s:
                    best_s = elapsed
                    best_lo = lo
                    best_right = right

        # Fallback: GPS series can fall a few metres short of the nominal distance
        # (e.g. 49 995 m for a 50K). If we're within 0.1% of the target, the full
        # series time is a valid split.
        if best_s is None and dists[-1] >= target_m * 0.999:
            best_s = times[-1] - times[0]
            best_lo = 0
            best_right = n - 1

        if best_s is not None:
            # Sanity: reject anything outside 3:00/mi–30:00/mi (catches corrupt GPS teleports)
            sec_per_mi = best_s / target_m * 1609.344
            if not (180 <= sec_per_mi <= 1800):
                continue
            pace_km, pace_mile = pace_from_seconds_meters(best_s, target_m)
            results.append({
                "label":        label,
                "target_m":     target_m,
                "elapsed_s":    best_s,
                "duration_fmt": fmt_duration(best_s),
                "pace_km":      pace_km,
                "pace_mile":    pace_mile,
                "sec_per_mi":   round(best_s / target_m * 1609.344, 1),
                "start_mi":     round(dists[best_lo] / 1609.344, 2),
                "end_mi":       round(dists[best_right] / 1609.344, 2),
            })

    return results


def fetch_all_best_splits(conn: sqlite3.Connection, races: list[dict]) -> dict[int, list[dict]]:
    cursor = conn.cursor()
    result = {}
    total = len(races)
    for i, race in enumerate(races, 1):
        aid = race["activity_id"]
        dist_m = race["distance"] or 0
        print(f"  [{i}/{total}] {race['activity_name'][:50]}", end="", flush=True)
        splits = best_splits_for_activity(cursor, aid, dist_m)
        result[aid] = splits
        print(f" -> {len(splits)} splits")
    return result


# ─────────────────────────────────────────────
# Pace series (for the overlay chart)
# ─────────────────────────────────────────────

def fetch_pace_series(
    cursor: sqlite3.Cursor,
    activity_id: int,
    interval_mi: float = 0.25,
    window_mi: float = 0.5,
) -> list[dict]:
    """
    Return [{x: dist_mi, y: sec_per_mi}, ...] sampled every interval_mi,
    smoothed with a window_mi centred rolling window.
    Uses actual timestamps so sparse older recordings are handled correctly.
    """
    times, dists = load_distance_series(cursor, activity_id)
    if not dists:
        return []

    n = len(dists)
    total_mi = dists[-1] / 1609.344
    half_w_m = window_mi / 2 * 1609.344

    result = []
    d_mi = interval_mi
    while d_mi <= total_mi + interval_mi / 4:
        d_m = d_mi * 1609.344
        lo_m = max(0.0, d_m - half_w_m)
        hi_m = min(dists[-1], d_m + half_w_m)

        lo_idx = bisect.bisect_left(dists, lo_m)
        hi_idx = bisect.bisect_right(dists, hi_m) - 1

        if hi_idx > lo_idx:
            cov = dists[hi_idx] - dists[lo_idx]
            el  = times[hi_idx] - times[lo_idx]   # actual seconds
            if el > 0 and cov > 100:
                pace_s = el / cov * 1609.344
                if 240 <= pace_s <= 1800:
                    result.append({"x": round(d_mi, 2), "y": round(pace_s, 1)})

        d_mi += interval_mi

    return result


def fetch_activity_chart_series(
    cursor: sqlite3.Cursor,
    activity_id: int,
    interval_mi: float = 0.1,
) -> list[dict]:
    """
    Return 0.1-mile interval data for the activity bar chart:
    [{x: dist_mi, pace: sec_per_mi, hr: avg_bpm_or_null}, ...]
    """
    t0_dt = _get_t0(cursor, activity_id)
    if t0_dt is None:
        return []

    times, dists = load_distance_series(cursor, activity_id)
    if not dists:
        return []

    hr_times, hr_vals = _load_named_series(cursor, activity_id, "heart_rate", t0_dt)

    total_mi = dists[-1] / 1609.344

    # Pre-compute (dist_mi, dist_m, elapsed_sec) at each 0.1-mile boundary
    boundaries: list[tuple[float, float, float]] = []
    d_mi = 0.0
    while d_mi <= total_mi + interval_mi / 4:
        d_m = min(d_mi * 1609.344, dists[-1])
        idx = bisect.bisect_left(dists, d_m)
        if idx == 0:
            t = times[0]
        elif idx >= len(dists):
            t = times[-1]
        else:
            d0, d1 = dists[idx - 1], dists[idx]
            t0_, t1_ = times[idx - 1], times[idx]
            frac = (d_m - d0) / (d1 - d0) if d1 > d0 else 0.0
            t = t0_ + frac * (t1_ - t0_)
        boundaries.append((round(d_mi, 10), d_m, t))
        d_mi = round(d_mi + interval_mi, 10)

    result = []
    for i in range(1, len(boundaries)):
        _, prev_d_m, prev_t = boundaries[i - 1]
        curr_d_mi, curr_d_m, curr_t = boundaries[i]

        dt = curr_t - prev_t
        dd = curr_d_m - prev_d_m
        if dt <= 0 or dd < 10:
            continue

        pace_s = dt / dd * 1609.344
        if not (180 <= pace_s <= 1800):
            continue

        avg_hr = None
        if hr_times:
            lo_i = bisect.bisect_left(hr_times, prev_t)
            hi_i = bisect.bisect_right(hr_times, curr_t)
            readings = hr_vals[lo_i:hi_i]
            if readings:
                avg_hr = round(sum(readings) / len(readings))

        result.append({
            "x":    round(curr_d_mi, 2),
            "pace": round(pace_s, 1),
            "hr":   avg_hr,
        })

    return result


def fetch_all_pace_series(conn: sqlite3.Connection, races: list[dict]) -> list[dict]:
    """
    Returns a list of chart-ready dicts, one per race, with colour assigned.
    Groups by dist_label so colours cycle within each distance category.
    """
    cursor = conn.cursor()
    # Assign colours per dist_label group so same-category races share a palette slice
    label_color_idx: dict[str, int] = {}
    label_counters:  dict[str, int] = {}

    # First pass: count races per label to figure palette sizing
    for r in races:
        lbl = r["distance_label"]
        label_counters[lbl] = label_counters.get(lbl, 0) + 1

    # Assign palette start per label (spread labels across palette)
    offset = 0
    for lbl in sorted(label_counters):
        label_color_idx[lbl] = offset
        offset += label_counters[lbl]

    label_used: dict[str, int] = {}
    result = []
    total = len(races)

    for i, race in enumerate(races, 1):
        aid  = race["activity_id"]
        lbl  = race["distance_label"]
        cidx = (label_color_idx.get(lbl, 0) + label_used.get(lbl, 0)) % len(PALETTE)
        label_used[lbl] = label_used.get(lbl, 0) + 1

        print(f"  pace [{i}/{total}] {race['activity_name'][:45]}", end="", flush=True)
        series = fetch_pace_series(cursor, aid)
        print(f" -> {len(series)} pts")

        result.append({
            "id":          aid,
            "name":        race["activity_name"],
            "date":        race["date"],
            "dist_label":  lbl,
            "color":       PALETTE[cidx],
            "series":      series,
        })

    return result


# ─────────────────────────────────────────────
# Per-mile splits
# ─────────────────────────────────────────────

def per_mile_splits_for_activity(
    cursor: sqlite3.Cursor, activity_id: int
) -> list[dict]:
    t0_dt = _get_t0(cursor, activity_id)
    times, dists = load_distance_series(cursor, activity_id)
    if len(dists) < 2 or t0_dt is None:
        return []

    hr_times,  hr_vals  = _load_named_series(cursor, activity_id, "heart_rate", t0_dt)
    alt_times, alt_vals = _load_named_series(cursor, activity_id, "enhanced_altitude", t0_dt)
    if not alt_vals:
        alt_times, alt_vals = _load_named_series(cursor, activity_id, "altitude", t0_dt)

    total_mi = dists[-1] / 1609.344
    splits = []
    prev_time = 0.0
    prev_alt_m = _interp(alt_times, alt_vals, 0.0) if alt_vals else None

    for mile_n in range(1, int(total_mi) + 1):
        target_m = mile_n * 1609.344
        if target_m > dists[-1]:
            break
        idx = bisect.bisect_left(dists, target_m)
        if idx == 0:
            t_at_mile = times[0]
        elif idx >= len(dists):
            t_at_mile = times[-1]
        else:
            d0, d1 = dists[idx - 1], dists[idx]
            t_lo, t_hi = times[idx - 1], times[idx]
            frac = (target_m - d0) / (d1 - d0) if d1 > d0 else 0.0
            t_at_mile = t_lo + frac * (t_hi - t_lo)

        split_s = t_at_mile - prev_time
        if split_s <= 0:
            prev_time = t_at_mile
            continue

        avg_hr = max_hr = None
        if hr_times:
            lo_i = bisect.bisect_left(hr_times, prev_time)
            hi_i = bisect.bisect_right(hr_times, t_at_mile)
            readings = hr_vals[lo_i:hi_i]
            if readings:
                avg_hr = round(sum(readings) / len(readings))
                max_hr = round(max(readings))

        elev_change_ft = None
        if alt_vals:
            curr_alt_m = _interp(alt_times, alt_vals, t_at_mile)
            if curr_alt_m is not None and prev_alt_m is not None:
                elev_change_ft = round((curr_alt_m - prev_alt_m) * 3.28084)
            prev_alt_m = curr_alt_m

        # Grade-adjusted pace: effort model 1 + 0.033×grade% (uphill costs more,
        # downhill recovers less), clamped to avoid nonsensical values on bad GPS.
        gap_s = None
        if elev_change_ft is not None:
            grade_pct = (elev_change_ft / 5280.0) * 100.0
            effort = max(0.5, min(2.0, 1.0 + 0.033 * grade_pct))
            gap_s = round(split_s / effort, 1)

        splits.append({
            "mile":           mile_n,
            "elapsed_s":      round(t_at_mile, 1),
            "split_s":        round(split_s, 1),
            "pace_fmt":       fmt_duration(split_s),
            "gap_s":          gap_s,
            "gap_fmt":        fmt_duration(gap_s) if gap_s is not None else "—",
            "avg_hr":         avg_hr,
            "max_hr":         max_hr,
            "elev_change_ft": elev_change_ft,
        })
        prev_time = t_at_mile

    return splits


def fetch_all_mile_splits(
    conn: sqlite3.Connection,
    races: list[dict],
    pace_series_data: list[dict],
) -> list[dict]:
    cursor = conn.cursor()
    color_map = {ps["id"]: ps["color"] for ps in pace_series_data}
    result = []
    total = len(races)
    for i, race in enumerate(races, 1):
        aid = race["activity_id"]
        print(f"  mile-splits [{i}/{total}] {race['activity_name'][:45]}", end="", flush=True)
        splits = per_mile_splits_for_activity(cursor, aid)
        print(f" -> {len(splits)} miles")
        if not splits:
            continue
        result.append({
            "id":           aid,
            "name":         race["activity_name"],
            "date":         race["date"],
            "dist_label":   race["distance_label"],
            "dist_miles":   round((race["distance"] or 0) / 1609.344, 2),
            "duration_fmt": race["duration_fmt"],
            "pace_mile":    race["pace_mile"],
            "color":        color_map.get(aid, "#64748b"),
            "splits":       splits,
        })
    return result


# ─────────────────────────────────────────────
# Races fetch
# ─────────────────────────────────────────────

def fetch_races(conn: sqlite3.Connection) -> list[dict]:
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT
            a.activity_id, a.activity_name, a.activity_type_key, a.start_ts,
            a.distance, a.duration, a.average_speed, a.max_speed, a.calories,
            a.average_hr, a.max_hr, a.location_name, a.pr,
            a.aerobic_training_effect, a.anaerobic_training_effect,
            a.hr_time_in_zone_1, a.hr_time_in_zone_2, a.hr_time_in_zone_3,
            a.hr_time_in_zone_4, a.hr_time_in_zone_5,
            r.vo2_max_value, r.avg_running_cadence, r.avg_vertical_oscillation,
            r.avg_ground_contact_time, r.avg_stride_length, r.elevation_gain,
            r.elevation_loss, r.avg_power, r.normalized_power,
            r.min_temperature, r.max_temperature
        FROM activity a
        LEFT JOIN running_agg_metrics r ON a.activity_id = r.activity_id
        WHERE a.event_type_key = 'race'
          AND a.activity_type_key IN ({})
        ORDER BY a.start_ts DESC
        """.format(",".join("?" * len(RUNNING_TYPES))),
        RUNNING_TYPES,
    )
    cols = [d[0] for d in cursor.description]
    rows = []
    for row in cursor.fetchall():
        r = dict(zip(cols, row))
        dist_m  = r["distance"] or 0
        dist_km = dist_m / 1000
        r["dist_km"]         = dist_km
        r["dist_km_fmt"]     = fmt_dist_km(dist_m)
        r["dist_miles_fmt"]  = fmt_dist_miles(dist_m)
        r["distance_label"]  = classify_distance(dist_km)
        r["pace_km"]         = fmt_pace(r["average_speed"])
        r["pace_mile"]       = fmt_pace_mile(r["average_speed"])
        r["max_pace_km"]     = fmt_pace(r["max_speed"])
        r["duration_fmt"]    = fmt_duration(r["duration"])
        r["date"]            = r["start_ts"][:10] if r["start_ts"] else "-"
        r["year"]            = r["start_ts"][:4]  if r["start_ts"] else "-"
        r["hr_zones"]        = hr_zone_pct(
            r["duration"],
            r["hr_time_in_zone_1"], r["hr_time_in_zone_2"], r["hr_time_in_zone_3"],
            r["hr_time_in_zone_4"], r["hr_time_in_zone_5"],
        )
        r["elev_gain_fmt"] = f"{int(r['elevation_gain'] * 3.28084)}ft" if r.get("elevation_gain") else "-"
        r["temp_fmt"] = (
            f"{r['min_temperature']:.0f}-{r['max_temperature']:.0f}C"
            if r.get("min_temperature") is not None and r.get("max_temperature") is not None
            else "-"
        )
        rows.append(r)
    return rows


def compute_prs(races: list[dict]) -> dict:
    buckets = {}
    for r in races:
        label = r["distance_label"]
        dur   = r["duration"]
        if dur and (label not in buckets or dur < buckets[label]["duration"]):
            buckets[label] = r
    return buckets


# ─────────────────────────────────────────────
# Site builder
# ─────────────────────────────────────────────
# Trophy room data
# ─────────────────────────────────────────────

WORLD_MAJORS = [
    ("Tokyo",   "Japan",          "TYO"),
    ("Boston",  "United States",  "BOS"),
    ("London",  "United Kingdom", "LON"),
    ("Berlin",  "Germany",        "BER"),
    ("Chicago", "United States",  "CHI"),
    ("New York","United States",  "NYC"),
]

def fetch_streaks(conn: sqlite3.Connection) -> dict:
    cursor = conn.cursor()
    in_clause = ",".join("?" * len(RUNNING_TYPES))
    cursor.execute(
        f"SELECT DISTINCT date(start_ts) as run_date FROM activity "
        f"WHERE activity_type_key IN ({in_clause}) AND start_ts IS NOT NULL "
        f"ORDER BY run_date",
        RUNNING_TYPES,
    )
    dates = [date.fromisoformat(r[0]) for r in cursor.fetchall() if r[0]]
    today = datetime.now().date()

    # Longest consecutive day streak
    longest_day, run, prev = 0, 0, None
    for d in dates:
        run = run + 1 if (prev is not None and (d - prev).days == 1) else 1
        longest_day = max(longest_day, run)
        prev = d

    # Current day streak (count backwards from today/yesterday)
    current_day, prev_d = 0, None
    for d in reversed(dates):
        if current_day == 0:
            if (today - d).days <= 1:
                current_day, prev_d = 1, d
            else:
                break
        elif (prev_d - d).days == 1:
            current_day += 1
            prev_d = d
        else:
            break

    # Week streaks (ISO weeks)
    week_set = sorted({d.isocalendar()[:2] for d in dates})
    longest_week, wk_run, prev_w = 0, 0, None
    for (iy, iw) in week_set:
        if prev_w is None:
            wk_run = 1
        else:
            py, pw = prev_w
            diff = (date.fromisocalendar(iy, iw, 1) - date.fromisocalendar(py, pw, 1)).days
            wk_run = wk_run + 1 if diff == 7 else 1
        longest_week = max(longest_week, wk_run)
        prev_w = (iy, iw)

    # Current week streak
    today_iy, today_iw, _ = today.isocalendar()
    today_mon = date.fromisocalendar(today_iy, today_iw, 1)
    current_week, prev_w = 0, None
    for (iy, iw) in reversed(week_set):
        this_mon = date.fromisocalendar(iy, iw, 1)
        if current_week == 0:
            if (today_mon - this_mon).days <= 7:
                current_week, prev_w = 1, (iy, iw)
            else:
                break
        else:
            py, pw = prev_w
            prev_mon = date.fromisocalendar(py, pw, 1)
            if (prev_mon - this_mon).days == 7:
                current_week += 1
                prev_w = (iy, iw)
            else:
                break

    return {
        "longest_day":   longest_day,
        "current_day":   current_day,
        "longest_week":  longest_week,
        "current_week":  current_week,
    }


def compute_repeat_courses(races: list[dict]) -> list[dict]:
    groups: dict[str, list] = defaultdict(list)
    for r in races:
        norm = re.sub(r'^\d{2,4}\s+', '', r["activity_name"])
        norm = re.sub(r'\s+\d{4}$', '', norm).strip()
        groups[norm].append(r)

    result = []
    for norm_name, group in groups.items():
        if len(group) < 2:
            continue
        group_sorted = sorted(group, key=lambda x: x["date"])
        best = min(group_sorted, key=lambda x: x["duration"] or float('inf'))
        first = group_sorted[0]
        imp_fmt = None
        if first["duration"] and best["duration"] and first["date"] != best["date"]:
            imp_s = first["duration"] - best["duration"]
            h, rem = divmod(int(imp_s), 3600)
            m, s = divmod(rem, 60)
            imp_fmt = f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
        result.append({
            "name":             norm_name,
            "count":            len(group),
            "distance_label":   group_sorted[0]["distance_label"],
            "improvement_fmt":  imp_fmt,
            "appearances": [
                {
                    "date":          r["date"],
                    "duration_fmt":  r["duration_fmt"],
                    "pace_mile":     r["pace_mile"],
                    "is_best":       r["date"] == best["date"] and r["duration_fmt"] == best["duration_fmt"],
                    "activity_id":   r["activity_id"],
                }
                for r in group_sorted
            ],
        })
    result.sort(key=lambda x: -x["count"])
    return result


def compute_monthly_race_calendar(races: list[dict]) -> list[dict]:
    counts: dict[int, int] = defaultdict(int)
    for r in races:
        counts[int(r["date"][5:7])] += 1
    max_count = max(counts.values()) if counts else 1
    return [
        {
            "month":  i,
            "name":   MONTH_NAMES[i - 1],
            "count":  counts[i],
            "pct":    round(counts[i] / max_count * 100),
        }
        for i in range(1, 13)
    ]


def compute_trophy_shelf(races: list[dict], prs: dict) -> list[dict]:
    MAJOR_KWS = ["tokyo", "boston", "london", "berlin", "chicago", "new york", "nyc"]
    pr_dates = {dist: r["date"] for dist, r in prs.items()}
    seen: set[str] = set()
    shelf = []
    for r in sorted(races, key=lambda x: x["date"]):
        dist     = r["distance_label"]
        name_low = r["activity_name"].lower()
        is_major = any(kw in name_low for kw in MAJOR_KWS)
        is_ultra = (r["distance"] or 0) > 60_000
        is_pr    = pr_dates.get(dist) == r["date"]
        is_first = dist not in seen
        seen.add(dist)
        if is_major:
            trophy_type, icon, label = "major",    "🏆",  "World Major"
        elif is_ultra:
            trophy_type, icon, label = "ultra",    "🎖️", "Ultra"
        elif is_pr:
            trophy_type, icon, label = "pr",       "🥇",  f"{dist} PR"
        elif is_first:
            trophy_type, icon, label = "first",    "⭐",  f"First {dist}"
        else:
            trophy_type, icon, label = "finisher", "🏅",  "Finisher"
        shelf.append({
            "name":           r["activity_name"],
            "date":           r["date"],
            "duration_fmt":   r["duration_fmt"],
            "distance_label": dist,
            "pace_mile":      r["pace_mile"],
            "trophy_type":    trophy_type,
            "icon":           icon,
            "label":          label,
        })
    return shelf


def compute_trophy_data(races: list[dict], prs: dict, all_splits: dict) -> dict:
    marathons = [r for r in races if 40_000 <= (r["distance"] or 0) <= 45_500]
    ultra     = [r for r in races if (r["distance"] or 0) > 45_500]

    # Marathon progression (chronological)
    marathon_prog = sorted(
        [{"name": r["activity_name"], "date": r["date"],
          "dur_s": r["duration"], "dur_fmt": r["duration_fmt"],
          "pace_mile": r["pace_mile"],
          "location": r["location_name"] or ""}
         for r in marathons if r["duration"]],
        key=lambda x: x["date"],
    )

    # Mark the PR in the progression
    if marathon_prog:
        pr_s = min(x["dur_s"] for x in marathon_prog)
        for x in marathon_prog:
            x["is_pr"] = (x["dur_s"] == pr_s)
        # improvement from first to PR
        first_s = marathon_prog[0]["dur_s"]
        pr_improvement_s = first_s - pr_s
        h = int(pr_improvement_s) // 3600
        m = (int(pr_improvement_s) % 3600) // 60
        s = int(pr_improvement_s) % 60
        improvement_fmt = f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
    else:
        improvement_fmt = "-"

    # World Marathon Majors: detect by name keywords
    MAJOR_KEYWORDS = {
        "Tokyo":    ["tokyo"],
        "Boston":   ["boston"],
        "London":   ["london"],
        "Berlin":   ["berlin"],
        "Chicago":  ["chicago"],
        "New York": ["new york", "nyc"],
    }
    completed_majors = {}
    for r in marathons:
        name_lower = r["activity_name"].lower()
        for major, kws in MAJOR_KEYWORDS.items():
            if any(kw in name_lower for kw in kws):
                if major not in completed_majors:
                    completed_majors[major] = {
                        "name": r["activity_name"],
                        "date": r["date"],
                        "dur_fmt": r["duration_fmt"],
                        "pace_mile": r["pace_mile"],
                    }

    majors_list = []
    for city, country, code in WORLD_MAJORS:
        done = completed_majors.get(city)
        majors_list.append({
            "city": city, "country": country, "code": code,
            "done": bool(done),
            "race": done,
        })

    # Summary stats
    total_dist_km  = sum((r["distance"] or 0) for r in races) / 1000
    total_time_hrs = sum((r["duration"] or 0) for r in races) / 3600
    years_active   = sorted({r["year"] for r in races})

    # PR records list for display
    pr_display = []
    for dist_label in ["5K", "10K", "Half Marathon", "Marathon", "50K"]:
        if dist_label in prs:
            pr = prs[dist_label]
            pr_display.append({
                "label":       dist_label,
                "dur_fmt":     pr["duration_fmt"],
                "pace_km":     pr["pace_km"],
                "pace_mile":   pr["pace_mile"],
                "race_name":   pr["activity_name"],
                "date":        pr["date"],
                "activity_id": pr["activity_id"],
            })

    # Notable milestones (hard-coded narrative facts from the data)
    milestones = []

    # First race ever
    if races:
        first = sorted(races, key=lambda r: r["date"])[0]
        milestones.append({
            "icon": "🏁",
            "title": "First race ever",
            "detail": f"{first['activity_name']} — {first['date']}",
            "sub": "A marathon. Started big.",
        })

    # First sub-4 marathon
    sub4 = [r for r in sorted(marathons, key=lambda x: x["date"])
            if r["duration"] and r["duration"] < 4 * 3600]
    if sub4:
        milestones.append({
            "icon": "⚡",
            "title": "First sub-4 marathon",
            "detail": f"{sub4[0]['activity_name']} — {sub4[0]['date']}",
            "sub": sub4[0]["duration_fmt"],
        })

    # Ultra finisher
    if ultra:
        u = sorted(ultra, key=lambda x: x["date"])[0]
        milestones.append({
            "icon": "🦁",
            "title": "Ultra runner",
            "detail": f"{u['activity_name']} — {u['date']}",
            "sub": f"{u['dist_km_fmt']} km · {u['duration_fmt']}",
        })

    # World Majors count
    n_done = sum(1 for m in majors_list if m["done"])
    milestones.append({
        "icon": "🌍",
        "title": f"{n_done} of 6 World Marathon Majors",
        "detail": ", ".join(m["city"] for m in majors_list if m["done"]),
        "sub": "Tokyo · London · Berlin · Chicago · New York",
    })

    # Marathon PR improvement
    if len(marathon_prog) > 1:
        milestones.append({
            "icon": "📈",
            "title": "Marathon improvement",
            "detail": f"{marathon_prog[0]['dur_fmt']}  →  {prs['Marathon']['duration_fmt']}",
            "sub": f"{improvement_fmt} faster since first marathon",
        })

    # Year-by-year breakdown
    year_races: dict[str, int]   = defaultdict(int)
    year_km:    dict[str, float] = defaultdict(float)
    for r in races:
        yr = r["year"]
        year_races[yr] += 1
        year_km[yr] += r["dist_km"]
    years_sorted = sorted(year_races)
    max_yr_races = max(year_races.values()) if year_races else 1
    year_stats = [
        {"year": yr, "count": year_races[yr], "km": round(year_km[yr]),
         "bar_pct": round(year_races[yr] / max_yr_races * 100)}
        for yr in years_sorted
    ]

    # Best split PRs across ALL races (fastest ever 1K, 1mi, 5K, 10K, 10mi, HM)
    split_prs: dict[str, dict] = {}
    for aid, splits in all_splits.items():
        race = next((r for r in races if r["activity_id"] == aid), None)
        if not race:
            continue
        for sp in splits:
            lbl = sp["label"]
            if lbl not in split_prs or sp["elapsed_s"] < split_prs[lbl]["elapsed_s"]:
                split_prs[lbl] = {**sp, "race_name": race["activity_name"], "date": race["date"], "activity_id": race["activity_id"]}

    return {
        "stats": {
            "total_races":    len(races),
            "total_dist_km":  round(total_dist_km, 1),
            "total_time_hrs": round(total_time_hrs, 1),
            "years_active":   len(years_active),
            "year_first":     years_active[0] if years_active else None,
            "year_last":      years_active[-1] if years_active else None,
            "marathons":      len(marathons),
            "ultras":         len(ultra),
        },
        "prs":             pr_display,
        "split_prs":       [split_prs[lbl] for _, lbl in SPLIT_TARGETS if lbl in split_prs],
        "marathon_prog":   marathon_prog,
        "majors":          majors_list,
        "majors_done":     n_done,
        "milestones":      milestones,
        "improvement_fmt": improvement_fmt,
        "year_stats":      year_stats,
        "repeat_courses":  compute_repeat_courses(races),
        "monthly_calendar": compute_monthly_race_calendar(races),
        "trophy_shelf":    compute_trophy_shelf(races, prs),
    }


# ─────────────────────────────────────────────
# Training blocks (16-week build-up per race)
# ─────────────────────────────────────────────

RUNNING_TYPES_TUPLE = RUNNING_TYPES  # alias

def _intensity_score(z1, z2, z3, z4, z5) -> float:
    """Weighted HR zone score: 0 (all Z1) → 1 (all Z5)."""
    total = (z1 or 0) + (z2 or 0) + (z3 or 0) + (z4 or 0) + (z5 or 0)
    if not total:
        return 0.0
    weighted = (z1 or 0)*1 + (z2 or 0)*2 + (z3 or 0)*3 + (z4 or 0)*4 + (z5 or 0)*5
    return round((weighted / total - 1) / 4, 3)  # normalise to [0, 1]


def fetch_training_blocks(conn: sqlite3.Connection, races: list[dict]) -> list[dict]:
    """
    For each race, return the 16 weekly training blocks that precede it.
    Week 16 = 15–9 weeks before race; Week 1 = race week (0–6 days before).
    Each block: {week_num, weeks_to_race, date_start, date_end, km, miles,
                 runs, training_load, intensity, runs_list}.
    """
    cursor = conn.cursor()
    in_clause = ",".join("?" * len(RUNNING_TYPES))
    result = []

    for race in races:
        race_date = datetime.strptime(race["date"], "%Y-%m-%d").date()
        weeks = []
        peak_km = 0.0

        for w in range(16):
            # w=0 → race week (days -6 … 0), w=15 → 16 weeks out
            day_end   = race_date - timedelta(days=w * 7)
            day_start = day_end - timedelta(days=6)
            date_start_s = day_start.isoformat()
            date_end_s   = (day_end + timedelta(days=1)).isoformat()  # exclusive upper

            cursor.execute(
                f"""
                SELECT
                    date(a.start_ts) as run_date,
                    a.activity_name,
                    a.distance,
                    a.duration,
                    a.average_hr,
                    a.activity_training_load,
                    a.hr_time_in_zone_1, a.hr_time_in_zone_2, a.hr_time_in_zone_3,
                    a.hr_time_in_zone_4, a.hr_time_in_zone_5,
                    a.aerobic_training_effect,
                    COALESCE(r.elevation_gain, 0) as elevation_gain,
                    a.event_type_key
                FROM activity a
                LEFT JOIN running_agg_metrics r ON a.activity_id = r.activity_id
                WHERE a.activity_type_key IN ({in_clause})
                  AND a.start_ts >= ?
                  AND a.start_ts < ?
                  AND a.activity_id != ?
                ORDER BY a.start_ts
                """,
                RUNNING_TYPES + (date_start_s, date_end_s, race["activity_id"]),
            )
            runs_raw = cursor.fetchall()

            total_km     = sum((r[2] or 0) for r in runs_raw) / 1000
            total_load   = sum((r[5] or 0) for r in runs_raw)
            total_dur    = sum((r[3] or 0) for r in runs_raw)
            total_elev_m = sum((r[12] or 0) for r in runs_raw)
            z1 = sum((r[6] or 0) for r in runs_raw)
            z2 = sum((r[7] or 0) for r in runs_raw)
            z3 = sum((r[8] or 0) for r in runs_raw)
            z4 = sum((r[9] or 0) for r in runs_raw)
            z5 = sum((r[10] or 0) for r in runs_raw)
            intensity = _intensity_score(z1, z2, z3, z4, z5)

            runs_list = [
                {
                    "date":          r[0],
                    "name":          r[1],
                    "km":            round((r[2] or 0) / 1000, 2),
                    "miles":         round((r[2] or 0) / 1609.344, 2),
                    "duration_fmt":  fmt_duration(r[3]),
                    "avg_hr":        int(r[4]) if r[4] else None,
                    "load":          round(r[5], 1) if r[5] else None,
                    "intensity":     _intensity_score(r[6], r[7], r[8], r[9], r[10]),
                    "aero_te":       round(r[11], 1) if r[11] else None,
                    "elev_ft":       round((r[12] or 0) * 3.28084),
                    "is_race":       r[13] == "race",
                }
                for r in runs_raw
            ]

            peak_km = max(peak_km, total_km)
            weeks.append({
                "week_num":      16 - w,
                "weeks_to_race": w,
                "date_start":    date_start_s,
                "date_end":      day_end.isoformat(),
                "km":            round(total_km, 2),
                "miles":         round(total_km / 1.60934, 2),
                "duration_s":    round(total_dur),
                "elev_gain_ft":  round(total_elev_m * 3.28084),
                "runs":          len(runs_raw),
                "training_load": round(total_load, 1),
                "intensity":     intensity,
                "runs_list":     runs_list,
            })

        # Re-order oldest→newest (week_num 1→16)
        weeks.sort(key=lambda x: x["week_num"])

        build_miles   = round(sum(w["miles"] for w in weeks), 1)
        build_time_s  = sum(w["duration_s"] for w in weeks)
        build_elev_ft = round(sum(w["elev_gain_ft"] for w in weeks))
        build_runs    = sum(w["runs"] for w in weeks)

        result.append({
            "race": {
                "activity_id":    race["activity_id"],
                "activity_name":  race["activity_name"],
                "date":           race["date"],
                "distance_label": race["distance_label"],
                "dist_km":        round(race["dist_km"], 2),
                "dist_miles":     round(race["dist_km"] / 1.60934, 1),
                "duration_fmt":   race["duration_fmt"],
                "pace_mile":      race["pace_mile"],
            },
            "weeks":         weeks,
            "peak_km":       round(peak_km, 2),
            "peak_miles":    round(peak_km / 1.60934, 2),
            "build_miles":   build_miles,
            "build_time_s":  build_time_s,
            "build_elev_ft": build_elev_ft,
            "build_runs":    build_runs,
        })

    return result


# ─────────────────────────────────────────────
# Activity heatmap + year summary
# ─────────────────────────────────────────────

def fetch_activity_heatmap(conn: sqlite3.Connection) -> dict:
    from collections import defaultdict
    cursor = conn.cursor()
    in_clause = ",".join("?" * len(RUNNING_TYPES))

    cursor.execute(
        f"""
        SELECT
            date(a.start_ts) as run_date,
            a.distance, a.duration, a.event_type_key,
            CAST(strftime('%w', datetime(a.start_ts,
                CAST(ROUND(COALESCE(a.timezone_offset_hours, 0)) AS TEXT) || ' hours'
            )) AS INTEGER) as local_dow,
            CAST(strftime('%m', a.start_ts) AS INTEGER) as run_month,
            CAST(strftime('%H', datetime(a.start_ts,
                CAST(ROUND(COALESCE(a.timezone_offset_hours, 0)) AS TEXT) || ' hours'
            )) AS INTEGER) as local_hour,
            COALESCE(r.elevation_gain, 0) as elevation_gain
        FROM activity a
        LEFT JOIN running_agg_metrics r ON a.activity_id = r.activity_id
        WHERE a.activity_type_key IN ({in_clause})
          AND a.start_ts IS NOT NULL
        ORDER BY a.start_ts
        """,
        RUNNING_TYPES,
    )
    rows = cursor.fetchall()

    def _blank():
        return {"miles": 0.0, "hours": 0.0, "runs": 0, "elev_ft": 0.0}

    weekly: dict = defaultdict(lambda: {
        "miles": 0.0, "hours": 0.0, "runs": 0, "days": set(), "date_start": None,
    })
    yearly: dict = defaultdict(lambda: {
        "runs": 0, "races": 0, "miles": 0.0, "hours": 0.0,
        "days": set(), "peak_week_miles": 0.0,
    })
    daily:      dict = defaultdict(lambda: {"miles": 0.0, "hours": 0.0, "runs": 0})
    dow_grid:   dict = defaultdict(lambda: defaultdict(_blank))   # {year: {0-6: {...}}}
    month_grid: dict = defaultdict(lambda: defaultdict(_blank))   # {year: {1-12: {...}}}
    hour_grid:  dict = defaultdict(lambda: defaultdict(_blank))   # {year: {0-23: {...}}}

    for run_date, distance, duration, event_type, local_dow, run_month, local_hour, elevation_gain in rows:
        if not run_date:
            continue
        dt   = datetime.strptime(run_date, "%Y-%m-%d").date()
        iso_yr, iso_wk, _ = dt.isocalendar()
        cal_yr  = dt.year
        miles   = (distance or 0) / 1609.344
        hrs     = (duration or 0) / 3600
        elev_ft = (elevation_gain or 0) * 3.28084

        wk = weekly[(iso_yr, iso_wk)]
        wk["miles"] += miles
        wk["hours"] += hrs
        wk["runs"]  += 1
        wk["days"].add(run_date)
        if wk["date_start"] is None or run_date < wk["date_start"]:
            wk["date_start"] = run_date

        yr = yearly[cal_yr]
        yr["runs"]  += 1
        yr["miles"] += miles
        yr["hours"] += hrs
        yr["days"].add(run_date)
        if event_type == "race":
            yr["races"] += 1

        d = daily[run_date]
        d["miles"] += miles
        d["hours"] += hrs
        d["runs"]  += 1

        if local_dow is not None:
            d = dow_grid[cal_yr][local_dow]
            d["miles"] += miles; d["hours"] += hrs; d["runs"] += 1; d["elev_ft"] += elev_ft
        if run_month:
            d = month_grid[cal_yr][run_month]
            d["miles"] += miles; d["hours"] += hrs; d["runs"] += 1; d["elev_ft"] += elev_ft
        if local_hour is not None:
            d = hour_grid[cal_yr][local_hour]
            d["miles"] += miles; d["hours"] += hrs; d["runs"] += 1; d["elev_ft"] += elev_ft

    # peak week per calendar year
    for (iso_yr, iso_wk), wk in weekly.items():
        yr = yearly[iso_yr]
        yr["peak_week_miles"] = max(yr["peak_week_miles"], wk["miles"])

    weeks_list = [
        {
            "year":       iso_yr,
            "week":       iso_wk,
            "date_start": wk["date_start"],
            "miles":      round(wk["miles"], 1),
            "hours":      round(wk["hours"], 2),
            "runs":       wk["runs"],
            "days":       len(wk["days"]),
        }
        for (iso_yr, iso_wk), wk in sorted(weekly.items())
    ]

    all_years = sorted(yearly.keys())
    year_stats = []
    for yr in all_years:
        d = yearly[yr]
        active_weeks = sum(
            1 for (iy, _), wk in weekly.items()
            if iy == yr and wk["miles"] > 0
        )
        year_stats.append({
            "year":             yr,
            "races":            d["races"],
            "runs":             d["runs"],
            "miles":            round(d["miles"], 1),
            "hours":            round(d["hours"], 1),
            "days_active":      len(d["days"]),
            "avg_miles_week":   round(d["miles"] / max(active_weeks, 1), 1),
            "peak_week_miles":  round(d["peak_week_miles"], 1),
            "active_weeks":     active_weeks,
        })

    def _serialize_grid(grid: dict) -> dict:
        return {
            str(yr): {
                str(k): {kk: round(vv, 1) for kk, vv in cell.items()}
                for k, cell in yr_data.items()
            }
            for yr, yr_data in sorted(grid.items())
        }

    # Cross-year totals for stat callouts
    dow_totals   = defaultdict(_blank)
    month_totals = defaultdict(_blank)
    hour_totals  = defaultdict(_blank)
    for yr_data in dow_grid.values():
        for k, d in yr_data.items():
            for f in ("miles", "hours", "runs", "elev_ft"):
                dow_totals[k][f] += d[f]
    for yr_data in month_grid.values():
        for k, d in yr_data.items():
            for f in ("miles", "hours", "runs", "elev_ft"):
                month_totals[k][f] += d[f]
    for yr_data in hour_grid.values():
        for k, d in yr_data.items():
            for f in ("miles", "hours", "runs", "elev_ft"):
                hour_totals[k][f] += d[f]

    DOW_LABELS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
    MONTH_LBL  = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
    def _hr_lbl(h):
        if h == 0:  return "12a"
        if h < 12:  return f"{h}a"
        if h == 12: return "12p"
        return f"{h - 12}p"

    best_week = max(weeks_list, key=lambda w: w["miles"]) if weeks_list else {}

    mad = max(dow_totals, key=lambda k: dow_totals[k]["runs"]) if dow_totals else 0
    mam = max(month_totals, key=lambda k: month_totals[k]["runs"]) if month_totals else 1
    phr = max(hour_totals, key=lambda k: hour_totals[k]["runs"]) if hour_totals else 0

    hm_stats = {
        "most_active_dow":   {"dow": mad, "label": DOW_LABELS[mad], "runs": dow_totals[mad]["runs"]},
        "most_active_month": {"month": mam, "label": MONTH_LBL[mam - 1], "runs": month_totals[mam]["runs"]},
        "peak_hour":         {"hour": phr, "label": _hr_lbl(phr), "runs": hour_totals[phr]["runs"]},
        "most_active_week":  {
            "year": best_week.get("year"), "week": best_week.get("week"),
            "miles": best_week.get("miles"), "date_start": best_week.get("date_start"),
        },
    }

    days_list = [
        {"date": dt, "miles": round(d["miles"], 2), "runs": d["runs"]}
        for dt, d in sorted(daily.items())
    ]

    return {
        "weeks":       weeks_list,
        "year_stats":  year_stats,
        "days_list":   days_list,
        "dow_grid":    _serialize_grid(dow_grid),
        "month_grid":  _serialize_grid(month_grid),
        "hour_grid":   _serialize_grid(hour_grid),
        "hm_stats":    hm_stats,
    }


# ─────────────────────────────────────────────
# Activity map + geographic achievements
# ─────────────────────────────────────────────

MAP_TYPE_COLORS = {
    "running":            "#10b981",
    "road_running":       "#10b981",
    "treadmill_running":  "#34d399",
    "track_running":      "#4ade80",
    "trail_running":      "#84cc16",
    "ultra_run":          "#f59e0b",
    "cycling":            "#00d4ff",
    "indoor_cycling":     "#0ea5e9",
    "open_water_swimming":"#22d3ee",
    "swimming":           "#22d3ee",
    "golf":               "#8b5cf6",
    "hiking":             "#a3e635",
    "walking":            "#94a3b8",
    "flying":             "#f97316",
}
MAP_TYPE_COLOR_DEFAULT = "#64748b"

# (name, min_lat, max_lat, min_lon, max_lon)  — ordered specific → broad
COUNTRY_BOXES = [
    ("New Caledonia", -23.0, -19.0, 163.0, 170.0),
    ("New Zealand",   -47.0, -34.0, 165.0, 179.0),
    ("Australia",     -45.0, -10.0, 112.0, 155.0),
    ("Japan",          24.0,  47.0, 122.0, 147.0),
    ("UK",             49.0,  61.0,  -9.0,   2.1),
    ("Austria",        46.4,  49.0,   9.5,  17.2),
    ("Germany",        47.2,  55.1,   5.8,  15.1),
    ("France",         41.3,  51.1,  -5.2,   9.6),
    ("Mexico",         14.0,  29.5, -118.5, -86.5),
    ("USA",            18.0,  72.0, -180.0, -65.0),
    ("Canada",         41.5,  84.0, -141.0, -52.0),
]

# (name, min_lat, max_lat, min_lon, max_lon)
US_STATE_BOXES = [
    ("Hawaii",           18.9, 22.3, -160.3, -154.8),
    ("Alaska",           54.0, 71.5, -168.0, -130.0),
    ("Florida",          24.4, 31.1,  -87.6,  -80.0),
    ("Texas",            25.8, 36.6, -106.7,  -93.5),
    ("Louisiana",        28.9, 33.1,  -94.1,  -88.7),
    ("Mississippi",      30.1, 35.1,  -91.7,  -88.1),
    ("Alabama",          30.1, 35.1,  -88.5,  -84.9),
    ("Georgia",          30.3, 35.1,  -85.7,  -80.8),
    ("South Carolina",   31.9, 35.2,  -83.4,  -78.5),
    ("North Carolina",   33.8, 36.6,  -84.3,  -75.5),
    ("Tennessee",        34.9, 36.7,  -90.3,  -81.6),
    ("Virginia",         36.5, 39.5,  -83.7,  -75.2),
    ("West Virginia",    37.2, 40.6,  -82.6,  -77.7),
    ("Kentucky",         36.5, 39.1,  -89.6,  -81.9),
    ("Arkansas",         33.0, 36.5,  -94.6,  -89.6),
    ("Missouri",         36.0, 40.7,  -95.8,  -89.1),
    ("Illinois",         36.9, 42.5,  -91.5,  -87.0),
    ("Indiana",          37.8, 41.8,  -88.1,  -84.8),
    ("Ohio",             38.4, 42.3,  -84.8,  -80.5),
    ("Michigan",         41.7, 48.3,  -90.4,  -82.4),
    ("Wisconsin",        42.5, 47.1,  -92.9,  -86.2),
    ("Minnesota",        43.5, 49.4,  -97.2,  -89.5),
    ("Iowa",             40.4, 43.5,  -96.6,  -90.1),
    ("Kansas",           36.9, 40.1, -102.1,  -94.6),
    ("Nebraska",         40.0, 43.0, -104.1,  -95.3),
    ("Oklahoma",         33.6, 37.0, -103.0,  -94.4),
    ("New Mexico",       31.3, 37.1, -109.1, -103.0),
    ("Colorado",         36.9, 41.1, -109.1, -102.0),
    ("Wyoming",          41.0, 45.1, -111.1, -104.1),
    ("South Dakota",     42.5, 45.9, -104.1,  -96.4),
    ("North Dakota",     45.9, 49.0, -104.1,  -96.6),
    ("Montana",          44.4, 49.1, -116.1, -104.0),
    ("Idaho",            41.9, 49.1, -117.2, -111.0),
    ("Utah",             36.9, 42.1, -114.1, -109.0),
    ("Arizona",          31.3, 37.1, -114.8, -109.0),
    ("Nevada",           35.0, 42.1, -120.0, -114.0),
    ("California",       32.5, 42.1, -124.5, -114.1),
    ("Oregon",           41.9, 46.3, -124.6, -116.5),
    ("Washington",       45.5, 49.1, -124.8, -116.9),
    ("Pennsylvania",     39.7, 42.3,  -80.5,  -74.7),
    ("New York",         40.4, 45.1,  -79.8,  -71.8),
    ("New Jersey",       38.9, 41.4,  -75.6,  -73.9),
    ("Delaware",         38.4, 39.8,  -75.8,  -75.0),
    ("Maryland",         37.9, 39.7,  -79.5,  -74.9),
    ("Connecticut",      40.9, 42.1,  -73.7,  -71.8),
    ("Rhode Island",     41.1, 42.1,  -71.9,  -71.1),
    ("Massachusetts",    41.2, 42.9,  -73.5,  -69.9),
    ("Vermont",          42.7, 45.0,  -73.4,  -71.5),
    ("New Hampshire",    42.7, 45.3,  -72.6,  -70.6),
    ("Maine",            43.1, 47.5,  -71.1,  -67.0),
]

COUNTRY_CONTINENT = {
    "USA":           "North America",
    "Canada":        "North America",
    "Mexico":        "North America",
    "UK":            "Europe",
    "France":        "Europe",
    "Germany":       "Europe",
    "Austria":       "Europe",
    "Japan":         "Asia",
    "Australia":     "Oceania",
    "New Zealand":   "Oceania",
    "New Caledonia": "Oceania",
}


def _detect_country(lat: float, lon: float) -> str | None:
    for name, min_lat, max_lat, min_lon, max_lon in COUNTRY_BOXES:
        if min_lat <= lat <= max_lat and min_lon <= lon <= max_lon:
            return name
    return None


def _detect_us_state(lat: float, lon: float) -> str | None:
    for name, min_lat, max_lat, min_lon, max_lon in US_STATE_BOXES:
        if min_lat <= lat <= max_lat and min_lon <= lon <= max_lon:
            return name
    return None


def fetch_activity_map_data(conn: sqlite3.Connection) -> dict:
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT activity_name, activity_type_key, start_latitude, start_longitude,
               start_ts, distance, event_type_key
        FROM activity
        WHERE start_latitude IS NOT NULL
          AND ABS(start_latitude) > 0.5
          AND ABS(start_longitude) > 0.5
        ORDER BY start_ts
        """
    )
    rows = cursor.fetchall()

    type_set: list[str] = sorted({r[1] or "other" for r in rows})
    type_idx  = {t: i for i, t in enumerate(type_set)}
    type_colors = [MAP_TYPE_COLORS.get(t, MAP_TYPE_COLOR_DEFAULT) for t in type_set]

    points: list[list] = []
    countries: dict[str, int] = defaultdict(int)
    states:    set[str]       = set()
    continents: set[str]      = set()
    hemispheres: set[str]     = set()

    for name, atype, lat, lon, ts, dist, event in rows:
        atype  = atype or "other"
        is_race = 1 if event == "race" else 0
        miles   = round((dist or 0) / 1609.344, 1)
        dt      = ts[:10] if ts else ""
        short_name = (name or "")[:60]
        points.append([round(lat, 5), round(lon, 5), type_idx.get(atype, 0), is_race, miles, dt, short_name])

        if lat > 0: hemispheres.add("North")
        if lat < 0: hemispheres.add("South")
        if lon > 0: hemispheres.add("East")
        if lon < 0: hemispheres.add("West")

        country = _detect_country(lat, lon)
        if country:
            countries[country] += 1
            if country in COUNTRY_CONTINENT:
                continents.add(COUNTRY_CONTINENT[country])
            if country == "USA":
                st = _detect_us_state(lat, lon)
                if st:
                    states.add(st)

    countries_list = [
        {"name": k, "count": v, "continent": COUNTRY_CONTINENT.get(k, "Other")}
        for k, v in sorted(countries.items(), key=lambda x: -x[1])
    ]

    all_states = [s[0] for s in US_STATE_BOXES]

    return {
        "points":      points,
        "types":       type_set,
        "type_colors": type_colors,
        "achievements": {
            "countries":   len(countries),
            "states":      len(states),
            "continents":  len(continents),
            "hemispheres": len(hemispheres),
            "total":       len(points),
        },
        "hemispheres": sorted(hemispheres),
        "states":      sorted(states),
        "all_states":  all_states,
        "countries":   countries_list,
        "continents":  sorted(continents),
    }


# ─────────────────────────────────────────────
# AI Analysis (local Ollama)
# ─────────────────────────────────────────────

def _format_data_for_prompt(
    races: list[dict],
    training_blocks: list[dict],
    prs: dict,
    all_effort_splits: dict[int, list[dict]],
    all_activities: list[dict],
) -> str:
    lines = []

    years = sorted({r["year"] for r in races if r.get("year")})
    lines.append(f"Runner has {len(races)} races and {len(all_activities)} total logged activities "
                 f"from {years[0] if years else '?'} to {years[-1] if years else '?'}.\n")

    lines.append("## Personal Records by Distance (official races)")
    for dist, r in sorted(prs.items()):
        lines.append(f"  {dist}: {r['duration_fmt']} on {r['date']} ({r['activity_name']})")

    # All-time best splits across every activity (training + races)
    lines.append("\n## All-Time Best Splits (training runs and races combined)")
    act_by_id = {a["activity_id"]: a for a in all_activities}
    for _, label in SPLIT_TARGETS:
        best_spm = None
        best_act = None
        for aid, splits in all_effort_splits.items():
            for sp in splits:
                if sp["label"] == label:
                    spm = sp.get("sec_per_mi", 0)
                    if 180 <= spm <= 1800 and (best_spm is None or spm < best_spm):
                        best_spm = spm
                        best_act = act_by_id.get(aid)
        if best_spm and best_act:
            m, s = int(best_spm // 60), int(best_spm % 60)
            lines.append(f"  {label}: {m}:{s:02d}/mi  ({best_act['activity_name']}, {best_act['date']})")

    # Volume peaks across all activities
    sorted_acts = sorted(all_activities, key=lambda a: a["date"])
    lines.append("\n## Volume Peaks (all activities)")
    longest = max(all_activities, key=lambda a: a.get("distance") or 0, default=None)
    if longest:
        lines.append(f"  Longest single run: {longest['miles']} mi  ({longest['activity_name']}, {longest['date']})")
    most_elev = max(all_activities, key=lambda a: a.get("elevation_gain") or 0, default=None)
    if most_elev and most_elev.get("elevation_gain"):
        lines.append(f"  Most elevation in one run: {int(most_elev['elevation_gain'] * 3.28084)} ft  ({most_elev['activity_name']}, {most_elev['date']})")
    total_miles = round(sum(a.get("miles", 0) for a in all_activities), 0)
    lines.append(f"  Lifetime miles (all activities): {int(total_miles)} mi")

    # Milestones
    lines.append("\n## Milestone Activities")
    MILE_MILESTONES = [100, 250, 500, 1000, 2500, 5000]
    RUN_MILESTONES  = [1, 50, 100, 250, 500, 1000]
    cum_miles = cum_runs = 0
    for act in sorted_acts:
        pm, pr = cum_miles, cum_runs
        cum_miles += act.get("miles", 0)
        cum_runs  += 1
        for m in MILE_MILESTONES:
            if pm < m <= cum_miles:
                lines.append(f"  {m:,}th mile reached: {act['activity_name']} on {act['date']}")
        for r in RUN_MILESTONES:
            if pr < r <= cum_runs:
                lines.append(f"  Run #{r:,}: {act['activity_name']} on {act['date']}")

    lines.append("\n## Complete Race List (oldest first)")
    for r in sorted(races, key=lambda x: x["date"]):
        elev = r.get("elev_gain_fmt", "-")
        loc  = r.get("location_name") or ""
        hr   = f", {int(r['average_hr'])} bpm avg HR" if r.get("average_hr") else ""
        lines.append(
            f"  {r['date']} | {r['activity_name']} | {r['distance_label']} "
            f"| {r['duration_fmt']} | {r['pace_mile']}/mi"
            + (f" | {loc}" if loc else "")
            + (f" | {elev} gain" if elev and elev != "-" else "")
            + hr
        )

    lines.append("\n## 16-Week Training Build Summaries (per race)")
    for block in training_blocks:
        r = block["race"]
        lines.append(
            f"  {r['date']} {r['activity_name']}: "
            f"{block['build_miles']} mi over 16 wks, "
            f"peak {block['peak_miles']} mi/wk, "
            f"{block['build_runs']} runs, "
            f"{block['build_elev_ft']} ft total climb"
        )

    return "\n".join(lines)


def _call_ollama(prompt: str) -> str:
    payload = json.dumps({
        "model": OLLAMA_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
    }).encode("utf-8")

    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/chat",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=600) as resp:
        data = json.loads(resp.read().decode("utf-8"))
        return data["message"]["content"]


def _calorie_stratum_index(calories: int) -> int:
    """Return the stratum index for a calorie count."""
    bounds = [s[0] for s in CALORIE_STRATA]
    idx = bisect.bisect_right(bounds, calories) - 1
    return max(0, min(idx, len(CALORIE_STRATA) - 1))


def generate_calorie_strata() -> list[dict]:
    """Return a list of {healthy, unhealthy} dicts for each calorie stratum.

    Results are cached in ai-calorie-strata-cache.json. Delete that file
    (or bump CALORIE_STRATA_VERSION) to regenerate.
    """
    cache: dict = {}
    if AI_CALORIE_STRATA_CACHE_PATH.exists():
        cache = json.loads(AI_CALORIE_STRATA_CACHE_PATH.read_text(encoding="utf-8"))

    if cache.get("version") == CALORIE_STRATA_VERSION and len(cache.get("strata", [])) == len(CALORIE_STRATA):
        print(f"  Using cached calorie strata (delete {AI_CALORIE_STRATA_CACHE_PATH.name} to refresh)")
        return cache["strata"]

    strata_results: list[dict] = cache.get("strata", [{}] * len(CALORIE_STRATA))
    # Make a mutable copy preserving any already-generated strata
    strata_results = list(strata_results)
    while len(strata_results) < len(CALORIE_STRATA):
        strata_results.append({})

    updated = False
    for i, (_, label) in enumerate(CALORIE_STRATA):
        if strata_results[i].get("healthy") and strata_results[i].get("unhealthy"):
            continue
        prompt = (
            f"A runner burned {label}. "
            "Give me exactly two food equivalents separated by a pipe character (|):\n"
            "First: a virtuous, healthy food that totals those calories — something you'd feel wholesome eating.\n"
            "Second: an outrageously indulgent, unhealthy food that totals those calories — something delightfully terrible.\n"
            "Rules: be specific with quantities and brands, be witty and a little absurd, no preamble, no quotes, no labels.\n"
            "Example format: 2.5 cups of kale salad with lemon tahini|one Wendy's Baconator plus a large frosty\n"
            "Respond with ONLY: healthy_text|unhealthy_text"
        )
        try:
            raw = _call_ollama(prompt).strip()
            if "|" in raw:
                parts = raw.split("|", 1)
                healthy = parts[0].strip().strip('"')
                unhealthy = parts[1].strip().strip('"')
            else:
                # Fallback: use full text as unhealthy, blank healthy
                healthy = ""
                unhealthy = raw.strip('"')
            strata_results[i] = {"healthy": healthy, "unhealthy": unhealthy}
            updated = True
            print(f"    [{i+1}/{len(CALORIE_STRATA)}] {label}: healthy='{healthy[:40]}...' unhealthy='{unhealthy[:40]}...'")
        except Exception as e:
            print(f"    [{i+1}/{len(CALORIE_STRATA)}] {label}: FAILED ({e})")

    if updated:
        cache = {"version": CALORIE_STRATA_VERSION, "strata": strata_results}
        AI_CALORIE_STRATA_CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")

    return strata_results


def get_calorie_pair(calories: int, strata: list[dict]) -> tuple[str, str] | None:
    """Return (healthy, unhealthy) strings for a calorie count, or None if unavailable."""
    if not calories or calories < 50 or not strata:
        return None
    idx = _calorie_stratum_index(calories)
    entry = strata[idx] if idx < len(strata) else {}
    h, u = entry.get("healthy", ""), entry.get("unhealthy", "")
    if h or u:
        return (h, u)
    return None


def generate_ai_analysis(
    races: list[dict],
    training_blocks: list[dict],
    prs: dict,
    all_effort_splits: dict[int, list[dict]],
    all_activities: list[dict],
) -> dict:
    cache_version = "v2-splits"
    if AI_CACHE_PATH.exists():
        cached = json.loads(AI_CACHE_PATH.read_text(encoding="utf-8"))
        if cached.get("race_count") == len(races) and cached.get("cache_version") == cache_version:
            print("\nUsing cached AI analysis (delete ai-analysis-cache.json to refresh)")
            return cached

    print(f"\nGenerating AI analysis with Ollama ({OLLAMA_MODEL})... (this may take a few minutes)")

    data_text = _format_data_for_prompt(races, training_blocks, prs, all_effort_splits, all_activities)

    prompt = f"""You are a thoughtful running coach and data analyst. Below is the complete race and training history for a runner. Write a rich, personal narrative analysis in markdown. The data includes not just official race results but all-time best split times achieved in training and races combined, volume peaks, and career milestones — use all of this to paint a complete picture. Given their present age (45 in 2026), sex (male), and history, how do they compare to typical age-group performance curves?

Structure your response with these exact sections (use ## headers):

## The Journey
A narrative overview of their running story — when they started, key turning points, the arc of improvement or change over the years. Reference milestone activities (100th run, 1,000th mile, etc.) where meaningful. Write as if speaking directly to the runner ("you").

## Year by Year
A brief summary for each active year — what they raced, how they performed, any notable themes.

## Distance Breakdown
For each distance (5K through Marathon and beyond), analyze their progression. Where relevant, compare official race PRs to their all-time best training splits — gaps here reveal untapped potential or pacing conservatism in races.

## Standout Races
Pick 5–8 races that were especially significant — PRs, breakthrough performances, tough days, or memorable events. Explain why each one matters in the context of the full career arc.

## Best Split Analysis
Using the all-time best splits data (training + races), what does the runner's speed profile look like across distances? How do their best training splits compare to race PRs? What does this suggest about their race-day execution vs. their actual fitness ceiling?

## Pacing & Race Strategy
What patterns emerge from their paces? Any evolution in race strategy over time? What does this suggest about their strengths and areas for growth?

## Training Observations
Based on their 16-week build data before each race, what patterns do you see? Which builds led to their best races? Any observations about volume, intensity, or peaking?

## Looking Ahead
Encouragement and observations based on where they are now and where they might go next. Be specific about what training or racing changes could unlock new performances.

---
RUNNER DATA:
{data_text}
---

Write in an engaging, warm, analytical tone. Be specific — reference actual race names, dates, times, and split numbers. This analysis should feel personal and insightful, not generic."""

    try:
        content = _call_ollama(prompt)
    except urllib.error.URLError as e:
        content = f"## Could not connect to Ollama\n\nMake sure Ollama is running (`ollama serve`) and the model `{OLLAMA_MODEL}` is pulled (`ollama pull {OLLAMA_MODEL}`).\n\nError: {e}"

    result = {
        "race_count":    len(races),
        "cache_version": cache_version,
        "generated_at":  datetime.now().strftime("%Y-%m-%d %H:%M"),
        "model":         OLLAMA_MODEL,
        "content":       content,
    }
    AI_CACHE_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print("  AI analysis complete.")
    return result


# ─────────────────────────────────────────────
# Per-race build analysis (Ollama)
# ─────────────────────────────────────────────

def _format_build_for_prompt(
    block: dict,
    prs: dict,
    notables: list[dict],
    race_splits: list[dict],
) -> str:
    race  = block["race"]
    weeks = block["weeks"]

    pr_entry = prs.get(race["distance_label"])
    if pr_entry:
        pr_time = pr_entry["duration_fmt"]
        is_pr   = pr_entry["date"] == race["date"]
        pr_note = f"PR at this distance: {pr_time} ({pr_entry['date']})"
        if is_pr:
            pr_note += " — THIS RACE IS THE PR"
        elif race["duration_fmt"] < pr_time:
            pr_note += " — this race was SLOWER than PR"
        else:
            pr_note += " — this race was FASTER than PR (new PR)"
    else:
        pr_note = "No prior PR on record for this distance"

    lines = [
        f"RACE: {race['activity_name']}",
        f"Date: {race['date']}  Distance: {race['distance_label']} ({race['dist_miles']} mi)",
        f"Result: {race['duration_fmt']} ({race['pace_mile']}/mi)",
        f"{pr_note}",
    ]

    # Best splits achieved within this race
    if race_splits:
        lines.append("")
        lines.append("BEST SPLITS WITHIN THIS RACE (fastest segment of each length found in GPS data):")
        for sp in race_splits:
            lines.append(f"  {sp['label']}: {sp['pace_mile']}/mi  ({sp['duration_fmt']})")

    # Context notables this race earned
    if notables:
        lines.append("")
        lines.append("CONTEXT — what made this race notable at the time:")
        for n in notables:
            value_part = f" ({n['value']})" if n.get("value") else ""
            lines.append(f"  • {n['label']} {n['window']}{value_part}")

    lines += [
        "",
        "BUILD SUMMARY (16 weeks before race):",
        f"  Total miles: {block['build_miles']}  Peak week: {block['peak_miles']} mi"
        f"  Total time: {round(block['build_time_s']/3600, 1)} hrs"
        f"  Total elevation: {block['build_elev_ft']} ft  Runs: {block['build_runs']}",
        "",
        "WEEK BY WEEK (W1=15 weeks out → W16=race week):",
    ]

    for w in weeks:
        label = f"W{w['week_num']:2d}"
        intensity_pct = round(w["intensity"] * 100) if w["intensity"] else 0
        race_runs = [r for r in w.get("runs_list", []) if r.get("is_race")]
        race_note = ""
        if race_runs:
            race_note = "  [RACE: " + ", ".join(
                f"{r['name']} ({r['miles']}mi {r['duration_fmt']})" for r in race_runs
            ) + "]"
        lines.append(
            f"  {label}: {w['miles']:5.1f} mi  {w['runs']} runs"
            f"  intensity {intensity_pct}%"
            f"  elev {w['elev_gain_ft']} ft"
            + (f"  load {w['training_load']:.0f}" if w["training_load"] else "")
            + race_note
        )

    return "\n".join(lines)


def generate_race_build_analyses(
    training_blocks: list[dict],
    prs: dict,
    all_notables: dict[int, list[dict]],
    all_effort_splits: dict[int, list[dict]],
) -> dict[str, str]:
    cache: dict = {}
    if AI_RACE_CACHE_PATH.exists():
        cache = json.loads(AI_RACE_CACHE_PATH.read_text(encoding="utf-8"))

    total   = len(training_blocks)
    updated = False

    for i, block in enumerate(training_blocks, 1):
        race_id  = str(block["race"]["activity_id"])
        race_aid = block["race"]["activity_id"]
        if race_id in cache:
            print(f"  [{i}/{total}] {block['race']['activity_name'][:45]} (cached)")
            continue

        print(f"  [{i}/{total}] {block['race']['activity_name'][:45]}...", end="", flush=True)

        notables    = all_notables.get(race_aid, [])
        race_splits = [
            sp for sp in all_effort_splits.get(race_aid, [])
            if 180 <= sp.get("sec_per_mi", 0) <= 1800
        ]
        data_text = _format_build_for_prompt(block, prs, notables, race_splits)

        prompt = f"""You are an experienced running coach. A runner has asked you to review their 16-week training block for a specific race. The data includes the race result, best GPS splits recorded within the race itself, context notables (what was remarkable about this race relative to their history), and a week-by-week training breakdown.

Consider they are a 45-year old (in 2026) male runner. Write a focused, actionable coaching assessment covering these points in 4–5 short paragraphs:

1. **Build Assessment** — Overall quality of this training block. Was the volume appropriate? Did intensity ramp sensibly?
2. **Strengths** — What did this build do well? (consistency, peak timing, variety, etc.)
3. **Race Execution** — How do the in-race GPS splits and notables illuminate what happened on race day? Did early segments look strong? Was there a fade? How does this connect to the training?
4. **Weaknesses / Gaps** — What was missing or suboptimal in the build? (under-training, over-training, abrupt changes, poor taper?)
5. **One Concrete Recommendation** — The single most impactful change for the next build toward this distance.

Be direct and specific. Reference actual numbers from the data. Keep it practical, not generic.

---
{data_text}
---"""

        try:
            content = _call_ollama(prompt)
            print(" done")
        except urllib.error.URLError as e:
            content = f"Could not reach Ollama: {e}"
            print(f" ERROR: {e}")

        cache[race_id] = content
        updated = True
        AI_RACE_CACHE_PATH.write_text(
            json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    if not updated:
        print("  All race analyses loaded from cache.")

    return cache


# ─────────────────────────────────────────────
# Best efforts across all activities
# ─────────────────────────────────────────────

def fetch_all_best_efforts(
    conn: sqlite3.Connection, activities: list[dict]
) -> dict[int, list[dict]]:
    """
    For every activity that has TS data, compute best splits at each SPLIT_TARGETS distance.
    Cache results in BEST_EFFORTS_CACHE_PATH keyed by activity_id; re-computes if update_ts changed.
    """
    raw_cache: dict = {}
    if BEST_EFFORTS_CACHE_PATH.exists():
        raw_cache = json.loads(BEST_EFFORTS_CACHE_PATH.read_text(encoding="utf-8"))

    cursor  = conn.cursor()
    result: dict[int, list[dict]] = {}
    updated = False
    computed = 0

    for act in activities:
        aid       = act["activity_id"]
        update_ts = act.get("update_ts") or ""
        key       = str(aid)

        if not act.get("ts_data_available"):
            result[aid] = []
            continue

        cached = raw_cache.get(key)
        if cached and cached.get("update_ts") == update_ts:
            result[aid] = cached["splits"]
            continue

        dist_m = act["distance"] or 0
        splits = best_splits_for_activity(cursor, aid, dist_m)
        result[aid] = splits
        raw_cache[key] = {"update_ts": update_ts, "splits": splits}
        updated  = True
        computed += 1
        if computed % 50 == 0:
            print(f"    {computed} computed so far…")
            BEST_EFFORTS_CACHE_PATH.write_text(
                json.dumps(raw_cache, ensure_ascii=False), encoding="utf-8"
            )

    if updated:
        BEST_EFFORTS_CACHE_PATH.write_text(
            json.dumps(raw_cache, ensure_ascii=False), encoding="utf-8"
        )
        print(f"  Best-efforts cache: {computed} new, {len(activities) - computed} loaded")
    else:
        print(f"  Best-efforts cache: all {len(activities)} loaded from cache")

    return result


def compute_best_efforts_by_distance(
    activities: list[dict],
    effort_splits: dict[int, list[dict]],
    top_n: int = 30,
) -> dict[str, list[dict]]:
    """
    For each SPLIT_TARGETS label, return the top_n efforts sorted by pace (fastest first).
    Each entry merges split stats with activity-level context (HR, load, elevation grade).
    """
    act_by_id = {a["activity_id"]: a for a in activities}
    by_label: dict[str, list] = {lbl: [] for _, lbl in SPLIT_TARGETS}

    for aid, splits in effort_splits.items():
        act = act_by_id.get(aid)
        if not act:
            continue
        dist_m    = act["distance"] or 0
        elev_gain = act.get("elevation_gain") or 0
        elev_per_mi = round(elev_gain * 3.28084 / (dist_m / 1609.344), 1) if dist_m > 500 else 0

        for sp in splits:
            lbl = sp["label"]
            if lbl not in by_label:
                continue
            if not (180 <= sp.get("sec_per_mi", 0) <= 1800):
                continue
            by_label[lbl].append({
                "activity_id":   aid,
                "name":          act["activity_name"],
                "date":          act["date"],
                "elapsed_s":     sp["elapsed_s"],
                "time_fmt":      sp["duration_fmt"],
                "pace_km":       sp["pace_km"],
                "pace_mile":     sp["pace_mile"],
                "sec_per_mi":    sp["sec_per_mi"],
                "avg_hr":        int(act["average_hr"]) if act.get("average_hr") else None,
                "load":          round(act["activity_training_load"]) if act.get("activity_training_load") else None,
                "elev_per_mi":   elev_per_mi,
                "activity_miles": act["miles"],
                "is_race":       act["is_race"],
                "type":          act["activity_type_key"],
            })

    return {
        lbl: sorted(efforts, key=lambda x: x["sec_per_mi"])[:top_n]
        for lbl, efforts in by_label.items()
    }


def compute_split_ranks(
    activity_id: int,
    activity_date: str,
    effort_splits: dict[int, list[dict]],
    activities: list[dict],
) -> dict[str, dict]:
    """
    For each split label in this activity, compute rank (1=fastest) across
    30-day, 1-year, and all-time windows relative to the activity's date.
    """
    act_by_id = {a["activity_id"]: a for a in activities}
    my_splits = {s["label"]: s.get("sec_per_mi") for s in effort_splits.get(activity_id, [])}
    if not my_splits:
        return {}

    try:
        activity_dt = datetime.strptime(activity_date, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return {}
    cutoff_30d = activity_dt - timedelta(days=30)
    cutoff_1yr = activity_dt - timedelta(days=365)

    label_efforts: dict[str, list] = {}
    for aid, splits in effort_splits.items():
        act = act_by_id.get(aid)
        if not act:
            continue
        try:
            act_dt = datetime.strptime(act["date"], "%Y-%m-%d").date()
        except (ValueError, TypeError):
            continue
        for sp in splits:
            lbl = sp["label"]
            spm = sp.get("sec_per_mi", 0)
            if not (180 <= spm <= 1800):
                continue
            if lbl not in label_efforts:
                label_efforts[lbl] = []
            label_efforts[lbl].append((act_dt, spm))

    result = {}
    for lbl, my_spm in my_splits.items():
        if not my_spm or not (180 <= my_spm <= 1800):
            continue
        entries = label_efforts.get(lbl, [])
        result[lbl] = {
            "all_time":  sum(1 for _, spm in entries if spm < my_spm) + 1,
            "year":      sum(1 for dt, spm in entries if spm < my_spm and dt >= cutoff_1yr) + 1,
            "month":     sum(1 for dt, spm in entries if spm < my_spm and dt >= cutoff_30d) + 1,
            "total_all": len(entries),
            "total_1yr": sum(1 for dt, _ in entries if dt >= cutoff_1yr),
            "total_30d": sum(1 for dt, _ in entries if dt >= cutoff_30d),
        }
    return result


# ─────────────────────────────────────────────
# All activities (log + per-page generation)
# ─────────────────────────────────────────────

def fetch_all_activities_list(conn: sqlite3.Connection) -> list[dict]:
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT
            a.activity_id, a.activity_name, a.activity_type_key, a.start_ts,
            a.distance, a.duration, a.average_speed, a.average_hr, a.max_hr,
            a.event_type_key, a.ts_data_available, a.update_ts,
            a.hr_time_in_zone_1, a.hr_time_in_zone_2, a.hr_time_in_zone_3,
            a.hr_time_in_zone_4, a.hr_time_in_zone_5,
            a.aerobic_training_effect, a.anaerobic_training_effect,
            a.activity_training_load, a.calories,
            a.start_latitude, a.start_longitude, a.location_name, a.pr,
            r.avg_running_cadence, r.avg_vertical_oscillation,
            r.avg_ground_contact_time, r.avg_stride_length,
            r.elevation_gain, r.elevation_loss,
            r.avg_power, r.normalized_power, r.vo2_max_value,
            r.min_temperature, r.max_temperature, r.avg_respiration_rate,
            a.timezone_offset_hours
        FROM activity a
        LEFT JOIN running_agg_metrics r ON a.activity_id = r.activity_id
        WHERE a.activity_type_key IN ({})
          AND a.start_ts IS NOT NULL
        ORDER BY a.start_ts DESC
        """.format(",".join("?" * len(RUNNING_TYPES))),
        RUNNING_TYPES,
    )
    cols = [d[0] for d in cursor.description]
    rows = []
    for row in cursor.fetchall():
        r = dict(zip(cols, row))
        dist_m = r["distance"] or 0
        r["miles"]         = round(dist_m / 1609.344, 2)
        r["dist_km"]       = round(dist_m / 1000, 2)
        r["activity_name"] = r["activity_name"] or f"Activity {r['activity_id']}"
        r["pace_mile"]     = fmt_pace_mile(r["average_speed"])
        r["pace_km"]       = fmt_pace(r["average_speed"])
        r["duration_fmt"]  = fmt_duration(r["duration"])
        r["date"]          = r["start_ts"][:10] if r["start_ts"] else "-"
        r["year"]          = int(r["start_ts"][:4]) if r["start_ts"] else 0
        r["month"]         = int(r["start_ts"][5:7]) if r["start_ts"] else 0
        r["is_race"]       = r["event_type_key"] == "race"
        r["hr_zones"]      = hr_zone_pct(
            r["duration"],
            r["hr_time_in_zone_1"], r["hr_time_in_zone_2"], r["hr_time_in_zone_3"],
            r["hr_time_in_zone_4"], r["hr_time_in_zone_5"],
        )
        r["elev_gain_fmt"] = f"{int(r['elevation_gain'] * 3.28084)}" if r.get("elevation_gain") else None
        # Local start hour (float) for "earliest run" notables
        if r.get("start_ts"):
            ts_clean = r["start_ts"].rstrip("Z").split(".")[0]  # "2024-01-14T08:30:00"
            dt_utc = datetime.fromisoformat(ts_clean)
            tz_h = float(r.get("timezone_offset_hours") or 0)
            dt_local = dt_utc + timedelta(hours=tz_h)
            r["local_hour"] = dt_local.hour + dt_local.minute / 60.0
        else:
            r["local_hour"] = None
        rows.append(r)
    return [r for r in rows if r["activity_id"] not in EXCLUDED_ACTIVITY_IDS]


def compute_achievements(
    activities: list[dict],
    effort_splits: dict[int, list[dict]],
    streaks: dict,
    heatmap: dict,
) -> dict:
    """
    No DB queries — everything comes from in-memory structures.
    """
    today      = date.today()
    act_by_id  = {a["activity_id"]: a for a in activities}
    sorted_acts = sorted(activities, key=lambda a: a["date"])

    WINDOWS = [
        ("30d",  "Last 30 days",   30),
        ("90d",  "Last 3 months",  90),
        ("180d", "Last 6 months", 180),
        ("365d", "Last year",     365),
        ("all",  "All time",      None),
    ]

    # ── Speed grid ──────────────────────────────────────────────────
    # Per distance: flat list of effort dicts sorted by pace (fastest first)
    dist_efforts: dict[str, list] = {lbl: [] for _, lbl in SPLIT_TARGETS}

    for aid, splits in effort_splits.items():
        act = act_by_id.get(aid)
        if not act:
            continue
        dist_m    = act.get("distance") or 0
        elev_gain = act.get("elevation_gain") or 0
        elev_pmi  = round(elev_gain * 3.28084 / max(dist_m / 1609.344, 0.1), 1) if dist_m > 100 else 0

        for sp in splits:
            lbl = sp["label"]
            if lbl not in dist_efforts:
                continue
            if not (180 <= sp.get("sec_per_mi", 0) <= 1800):
                continue
            dist_efforts[lbl].append({
                "activity_id": aid,
                "name":        act["activity_name"],
                "date":        act["date"],
                "time_fmt":    sp["duration_fmt"],
                "pace_mile":   sp["pace_mile"],
                "pace_km":     sp["pace_km"],
                "sec_per_mi":  sp["sec_per_mi"],
                "elapsed_s":   sp["elapsed_s"],
                "avg_hr":      int(act["average_hr"]) if act.get("average_hr") else None,
                "is_race":     act["is_race"],
                "elev_pmi":    elev_pmi,
                "act_miles":   act["miles"],
            })

    for lbl in dist_efforts:
        dist_efforts[lbl].sort(key=lambda x: x["sec_per_mi"])
        for i, e in enumerate(dist_efforts[lbl]):
            e["all_time_rank"] = i + 1

    speed_grid: dict[str, dict] = {}
    for _, lbl in SPLIT_TARGETS:
        all_efforts = dist_efforts.get(lbl, [])
        if not all_efforts:
            continue
        pr_spm = all_efforts[0]["sec_per_mi"]
        speed_grid[lbl] = {}
        for win_key, win_label, win_days in WINDOWS:
            if win_days is None:
                window = all_efforts
            else:
                cutoff = (today - timedelta(days=win_days)).isoformat()
                window = [e for e in all_efforts if e["date"] >= cutoff]
            if not window:
                speed_grid[lbl][win_key] = None
                continue
            best = window[0]
            speed_grid[lbl][win_key] = {
                **best,
                "window_label":  win_label,
                "pct_off_pr":    round((best["sec_per_mi"] - pr_spm) / pr_spm * 100, 1),
                "is_pr":         best["all_time_rank"] == 1,
                "effort_count":  len(window),
            }

    # ── Volume peaks ────────────────────────────────────────────────
    weeks      = heatmap.get("weeks", [])
    year_stats = heatmap.get("year_stats", [])

    monthly_acc: dict = defaultdict(lambda: {"miles": 0.0, "hours": 0.0, "runs": 0})
    for act in activities:
        ym = act["date"][:7]
        monthly_acc[ym]["miles"] += act["miles"]
        monthly_acc[ym]["hours"] += (act.get("duration") or 0) / 3600
        monthly_acc[ym]["runs"]  += 1
    monthly_list = sorted(
        [{"ym": k, "date_start": k + "-01", **v} for k, v in monthly_acc.items()],
        key=lambda m: m["ym"],
    )

    def _best_week(days=None):
        pool = weeks
        if days:
            cutoff = (today - timedelta(days=days)).isoformat()
            pool = [w for w in weeks if (w.get("date_start") or "") >= cutoff]
        return max(pool, key=lambda w: w.get("miles", 0)) if pool else None

    def _best_month(field, days=None):
        pool = monthly_list
        if days:
            cutoff = (today - timedelta(days=days)).isoformat()[:7]
            pool = [m for m in monthly_list if m["ym"] >= cutoff]
        return max(pool, key=lambda m: m.get(field, 0)) if pool else None

    volume = {
        "peak_week_all":   _best_week(),
        "peak_week_365d":  _best_week(365),
        "peak_week_90d":   _best_week(90),
        "peak_month_miles": _best_month("miles"),
        "peak_month_runs":  _best_month("runs"),
        "peak_year_miles":  max(year_stats, key=lambda y: y["miles"]) if year_stats else None,
        "peak_year_runs":   max(year_stats, key=lambda y: y["runs"])  if year_stats else None,
    }

    # ── Single-run records ───────────────────────────────────────────
    def _pick(key, pool, invert=False):
        valid = [a for a in pool if (a.get(key) or 0) > 0]
        if not valid:
            return None
        best = (min if invert else max)(valid, key=lambda a: a.get(key) or 0)
        return {
            "activity_id":    best["activity_id"],
            "name":           best["activity_name"],
            "date":           best["date"],
            "miles":          best["miles"],
            "duration_fmt":   best.get("duration_fmt"),
            "pace_mile":      best.get("pace_mile"),
            "is_race":        best.get("is_race", False),
            "value":          best.get(key),
        }

    def _pool(days=None):
        if not days:
            return activities
        cutoff = (today - timedelta(days=days)).isoformat()
        return [a for a in activities if a["date"] >= cutoff]

    single_run = {
        k: v for k, v in {
            "most_load_all":   _pick("activity_training_load", _pool()),
            "most_load_365d":  _pick("activity_training_load", _pool(365)),
            "most_elev_all":   _pick("elevation_gain", _pool()),
            "most_elev_365d":  _pick("elevation_gain", _pool(365)),
            "longest_all":     _pick("distance", _pool()),
            "longest_365d":    _pick("distance", _pool(365)),
            "fastest_avg_all": _pick("average_speed", _pool()),
        }.items() if v is not None
    }
    # Format values that need it
    for k in ("most_elev_all", "most_elev_365d"):
        if k in single_run and single_run[k]["value"]:
            single_run[k]["value_fmt"] = f"{int(single_run[k]['value'] * 3.28084)}ft"
    for k in ("longest_all", "longest_365d"):
        if k in single_run and single_run[k]["value"]:
            mi = single_run[k]["value"] / 1609.344
            single_run[k]["value_fmt"] = f"{mi:.1f} mi"
    for k in ("fastest_avg_all",):
        if k in single_run and single_run[k]["value"]:
            single_run[k]["value_fmt"] = fmt_pace_mile(single_run[k]["value"])

    # ── Milestones ───────────────────────────────────────────────────
    MILE_GATES  = [100, 250, 500, 1000, 2000, 3000, 5000, 7500, 10000]
    COUNT_GATES = [1, 10, 25, 50, 100, 200, 300, 500, 750, 1000]
    ELEV_GATES  = [100_000, 250_000, 500_000, 1_000_000]
    HOUR_GATES  = [500, 1000, 2500, 5000]
    RACE_GATES  = [5, 10, 25, 50, 100]
    SUB_PACE_GATES = [(480, "8:00"), (420, "7:00"), (360, "6:00"), (300, "5:00")]
    TYPE_ICONS  = {
        "trail_running":     "🏔️",
        "treadmill_running": "🏭",
        "track_running":     "🏟️",
        "road_running":      "🛣️",
        "ultra_run":         "🦁",
    }
    milestones    = []
    cum_miles     = 0.0
    cum_elev_ft   = 0.0
    cum_hours     = 0.0
    race_count    = 0
    seen_types: set = set()
    next_mi_gate  = 0
    next_ct_gate  = 0
    next_elev_gate = 0
    next_hr_gate  = 0
    next_race_gate = 0
    next_spg      = 0

    for i, act in enumerate(sorted_acts, 1):
        cum_miles   += act["miles"]
        cum_elev_ft += (act.get("elevation_gain") or 0) * 3.28084
        cum_hours   += (act.get("duration") or 0) / 3600
        if act.get("is_race"):
            race_count += 1

        if next_ct_gate < len(COUNT_GATES) and i == COUNT_GATES[next_ct_gate]:
            milestones.append({
                "type": "count", "icon": "🏃",
                "title": f"Run #{i:,}",
                "detail": act["activity_name"],
                "date": act["date"],
                "activity_id": act["activity_id"],
                "value": f"{i:,}",
            })
            next_ct_gate += 1

        while next_mi_gate < len(MILE_GATES) and cum_miles >= MILE_GATES[next_mi_gate]:
            milestones.append({
                "type": "miles", "icon": "🛣️",
                "title": f"{MILE_GATES[next_mi_gate]:,} total miles",
                "detail": act["activity_name"],
                "date": act["date"],
                "activity_id": act["activity_id"],
                "value": f"{cum_miles:.0f}",
            })
            next_mi_gate += 1

        while next_elev_gate < len(ELEV_GATES) and cum_elev_ft >= ELEV_GATES[next_elev_gate]:
            gate = ELEV_GATES[next_elev_gate]
            label = f"{gate // 1000:,}K ft" if gate < 1_000_000 else f"{gate // 1_000_000:,}M ft"
            milestones.append({
                "type": "elevation", "icon": "⛰️",
                "title": f"{gate:,} ft elevation climbed",
                "detail": act["activity_name"],
                "date": act["date"],
                "activity_id": act["activity_id"],
                "value": label,
            })
            next_elev_gate += 1

        while next_hr_gate < len(HOUR_GATES) and cum_hours >= HOUR_GATES[next_hr_gate]:
            gate = HOUR_GATES[next_hr_gate]
            milestones.append({
                "type": "hours", "icon": "⏱️",
                "title": f"{gate:,} hours on feet",
                "detail": act["activity_name"],
                "date": act["date"],
                "activity_id": act["activity_id"],
                "value": f"{gate:,}h",
            })
            next_hr_gate += 1

        if next_race_gate < len(RACE_GATES) and race_count == RACE_GATES[next_race_gate]:
            gate = RACE_GATES[next_race_gate]
            milestones.append({
                "type": "race_count", "icon": "🎽",
                "title": f"Race #{gate}",
                "detail": act["activity_name"],
                "date": act["date"],
                "activity_id": act["activity_id"],
                "value": f"{gate}",
            })
            next_race_gate += 1

        if next_spg < len(SUB_PACE_GATES) and act.get("average_speed") and act["average_speed"] > 0:
            spm = 1609.344 / act["average_speed"]
            while next_spg < len(SUB_PACE_GATES) and spm < SUB_PACE_GATES[next_spg][0]:
                _, pace_label = SUB_PACE_GATES[next_spg]
                milestones.append({
                    "type": "sub_pace", "icon": "⚡",
                    "title": f"First sub-{pace_label}/mi run",
                    "detail": f"{act['activity_name']} · {act['pace_mile']}/mi avg",
                    "date": act["date"],
                    "activity_id": act["activity_id"],
                    "value": f"<{pace_label}",
                })
                next_spg += 1

        atype = act["activity_type_key"]
        if atype not in seen_types:
            seen_types.add(atype)
            if atype in TYPE_ICONS:
                milestones.append({
                    "type": "first_type", "icon": TYPE_ICONS[atype],
                    "title": f"First {atype.replace('_', ' ').title()}",
                    "detail": act["activity_name"],
                    "date": act["date"],
                    "activity_id": act["activity_id"],
                    "value": act["date"][:4],
                })

    # ── Consecutive years streak milestones ──────────────────────────
    YEAR_STREAK_GATES = [2, 5, 10, 15, 20]
    years_with_runs = sorted({a["year"] for a in sorted_acts if a.get("year") and a["year"] > 0})
    if len(years_with_runs) >= 2:
        last_act_in_year: dict = {}
        for act in sorted_acts:
            yr = act.get("year")
            if yr and yr > 0:
                last_act_in_year[yr] = act
        streak = 1
        next_yr_gate = 0
        for idx in range(1, len(years_with_runs)):
            if years_with_runs[idx] == years_with_runs[idx - 1] + 1:
                streak += 1
            else:
                streak = 1
            while next_yr_gate < len(YEAR_STREAK_GATES) and streak >= YEAR_STREAK_GATES[next_yr_gate]:
                gate = YEAR_STREAK_GATES[next_yr_gate]
                yr = years_with_runs[idx]
                act = last_act_in_year.get(yr, sorted_acts[-1])
                start_yr = yr - gate + 1
                milestones.append({
                    "type": "year_streak", "icon": "📅",
                    "title": f"{gate} consecutive years running",
                    "detail": f"{start_yr}–{yr}",
                    "date": act["date"],
                    "activity_id": act["activity_id"],
                    "value": f"{gate}yr",
                })
                next_yr_gate += 1

    return {
        "speed_grid":   speed_grid,
        "dist_labels":  [lbl for _, lbl in SPLIT_TARGETS],
        "windows":      [{"key": k, "label": l} for k, l, _ in WINDOWS],
        "volume":       volume,
        "single_run":   single_run,
        "milestones":   sorted(milestones, key=lambda m: m["date"]),
        "streaks":      streaks,
        "total_miles":  round(sum(a["miles"] for a in activities), 1),
        "total_runs":   len(activities),
        "total_races":  sum(1 for a in activities if a["is_race"]),
    }


def compute_notables(
    activities: list[dict],
    effort_splits: dict[int, list[dict]],
) -> dict[int, list[dict]]:
    """
    Per-activity notables: rank-1 achievements within rolling time windows.
    For each metric, only the most impressive (broadest) window is awarded.
    Returns {activity_id: [{"key", "label", "icon", "window", "tier", "value"}, ...]}.
    """
    WINDOWS = [
        (None, "all-time",    "pr"),
        (365,  "in a year",   "hot"),
        (180,  "in 6 months", "good"),
        (90,   "in 3 months", "ok"),
        (30,   "in a month",  "muted"),
    ]

    sorted_acts = sorted(activities, key=lambda a: a["date"])
    dates       = [a["date"] for a in sorted_acts]

    # Best-split lookup: {aid: {label: sec_per_mi}} — sanity filtered
    split_lookup: dict[int, dict[str, float]] = {}
    for aid, splits in effort_splits.items():
        split_lookup[aid] = {}
        for sp in splits:
            spm = sp.get("sec_per_mi", 0)
            if 180 <= spm <= 1800:
                split_lookup[aid][sp["label"]] = spm

    notables: dict[int, list] = {a["activity_id"]: [] for a in activities}

    def get_window(act_date: str, window_days):
        if window_days is None:
            return sorted_acts
        cutoff = (date.fromisoformat(act_date) - timedelta(days=window_days)).isoformat()
        lo = bisect.bisect_left(dates, cutoff)
        hi = bisect.bisect_right(dates, act_date)
        return sorted_acts[lo:hi]

    def fmt_spm(spm: float) -> str:
        m, s = int(spm // 60), int(spm % 60)
        return f"{m}:{s:02d}/mi"

    def fmt_local_time(h_frac: float) -> str:
        h, m = int(h_frac), int((h_frac % 1) * 60)
        return f"{h % 12 or 12}:{m:02d} {'AM' if h < 12 else 'PM'}"

    # ── Generic field-based metric ────────────────────────────────────
    def award_field(key, label, icon, field, better, fmt, min_field=None, min_val=None):
        cmp_fn  = max if better == "max" else min
        is_best = (lambda v, b: v >= b) if better == "max" else (lambda v, b: v <= b)

        def ok(a):
            v = a.get(field)
            return v is not None and (not min_field or (a.get(min_field) or 0) >= min_val)

        for act in sorted_acts:
            if not ok(act):
                continue
            aid = act["activity_id"]
            val = act.get(field)
            for window_days, window_label, tier in WINDOWS:
                candidates = [wa.get(field) for wa in get_window(act["date"], window_days) if ok(wa)]
                if not candidates:
                    continue
                if is_best(val, cmp_fn(candidates)):
                    notables[aid].append({
                        "key": key, "label": label, "icon": icon,
                        "window": window_label, "tier": tier, "value": fmt(val),
                    })
                    break

    award_field("longest_run",       "Longest Run",           "↔",
                "distance", "max",    lambda v: f"{v/1000:.1f} km")
    award_field("longest_duration",  "Longest Duration",      "⏱",
                "duration", "max",    fmt_duration,            "duration", 300)
    award_field("most_elevation",    "Most Elevation Gain",   "↑",
                "elevation_gain", "max", lambda v: f"{int(v * 3.28084)} ft", "elevation_gain", 10)
    award_field("highest_load",      "Highest Training Load", "⚡",
                "activity_training_load", "max", lambda v: f"{v:.0f}",
                "activity_training_load", 1)
    award_field("most_calories",     "Most Calories",         "◈",
                "calories", "max",    lambda v: f"{int(v):,} kcal", "calories", 100)
    award_field("lowest_avg_hr",     "Lowest Avg HR",         "♡",
                "average_hr", "min",  lambda v: f"{v:.0f} bpm",  "distance", 5000)
    award_field("fastest_pace",      "Fastest Pace",          "▶",
                "average_speed", "max", fmt_pace_mile,            "distance", 5000)
    award_field("earliest_run",      "Earliest Run",          "◑",
                "local_hour", "min",  fmt_local_time)

    # ── Split metrics ─────────────────────────────────────────────────
    for _, dist_label in SPLIT_TARGETS:
        for act in sorted_acts:
            aid = act["activity_id"]
            val = split_lookup.get(aid, {}).get(dist_label)
            if val is None:
                continue
            for window_days, window_label, tier in WINDOWS:
                candidates = [
                    split_lookup.get(wa["activity_id"], {}).get(dist_label)
                    for wa in get_window(act["date"], window_days)
                ]
                candidates = [c for c in candidates if c is not None]
                if not candidates:
                    continue
                if val <= min(candidates):
                    notables[aid].append({
                        "key":    f"fastest_{dist_label.lower().replace(' ', '_')}",
                        "label":  f"Fastest {dist_label}",
                        "icon":   "⚡",
                        "window": window_label, "tier": tier,
                        "value":  fmt_spm(val),
                    })
                    break

    # ── Milestones ─────────────────────────────────────────────────────
    MILE_MILESTONES = [100, 250, 500, 1000, 2500, 5000, 10000]
    RUN_MILESTONES  = [1, 10, 25, 50, 100, 250, 500, 1000]
    cum_miles = cum_runs = 0
    for act in sorted_acts:
        aid = act["activity_id"]
        pm, pr = cum_miles, cum_runs
        cum_miles += act.get("miles", 0)
        cum_runs  += 1
        for m in MILE_MILESTONES:
            if pm < m <= cum_miles:
                notables[aid].append({
                    "key": "milestone_miles", "label": f"{m:,} Miles",
                    "icon": "◉", "window": "lifetime", "tier": "milestone", "value": "",
                })
        for r in RUN_MILESTONES:
            if pr < r <= cum_runs:
                notables[aid].append({
                    "key": "milestone_runs", "label": f"Run #{r:,}",
                    "icon": "#", "window": "lifetime", "tier": "milestone", "value": "",
                })

    return notables


def build_activity_pages(
    conn: sqlite3.Connection,
    activities: list[dict],
    env,
    all_notables: dict[int, list],
    calorie_strata: list[dict] | None = None,
    all_effort_splits: dict[int, list[dict]] | None = None,
) -> None:
    manifest: dict = {}
    if ACTIVITIES_MANIFEST_PATH.exists():
        raw = json.loads(ACTIVITIES_MANIFEST_PATH.read_text(encoding="utf-8"))
        if raw.get("_version") == ACTIVITIES_MANIFEST_VERSION:
            manifest = {k: v for k, v in raw.items() if k != "_version"}
        # version mismatch → fresh rebuild

    acts_dir = DIST_DIR / "activity"
    acts_dir.mkdir(exist_ok=True)

    tmpl   = env.get_template("activity.html")
    cursor = conn.cursor()

    # prev/next navigation (chronological order)
    sorted_ids = [str(a["activity_id"]) for a in sorted(activities, key=lambda x: x["date"])]
    id_to_idx  = {aid: i for i, aid in enumerate(sorted_ids)}

    built = skipped = 0
    total = len(activities)

    for i, act in enumerate(activities, 1):
        aid       = str(act["activity_id"])
        out_path  = acts_dir / f"{aid}.html"
        update_ts = act.get("update_ts") or ""

        if out_path.exists() and manifest.get(aid) == update_ts:
            skipped += 1
            continue

        name = (act['activity_name'] or f"Activity {act['activity_id']}")[:50]
        print(f"  [{i}/{total}] {name}", end="", flush=True)

        chart_series: list[dict] = []
        mile_splits: list[dict] = []
        activity_splits: list[dict] = []
        split_ranks: dict[str, dict] = {}
        if act.get("ts_data_available"):
            chart_series = fetch_activity_chart_series(cursor, int(aid))
            mile_splits  = per_mile_splits_for_activity(cursor, int(aid))
            dist_m = act.get("distance") or 0
            activity_splits = best_splits_for_activity(cursor, int(aid), dist_m)
            if all_effort_splits is not None:
                split_ranks = compute_split_ranks(
                    int(aid), act.get("date", ""), all_effort_splits, activities
                )

        idx     = id_to_idx.get(aid, -1)
        prev_id = sorted_ids[idx - 1] if idx > 0 else None
        next_id = sorted_ids[idx + 1] if 0 <= idx < len(sorted_ids) - 1 else None

        calorie_pair = get_calorie_pair(act.get("calories") or 0, calorie_strata or [])
        html = tmpl.render(
            activity=act,
            chart_series=chart_series,
            chart_series_json=json.dumps(chart_series),
            mile_splits=mile_splits,
            activity_splits=activity_splits,
            split_ranks=split_ranks,
            prev_id=prev_id,
            next_id=next_id,
            notables=all_notables.get(int(aid), []),
            calorie_healthy=calorie_pair[0] if calorie_pair else None,
            calorie_unhealthy=calorie_pair[1] if calorie_pair else None,
        )
        out_path.write_text(html, encoding="utf-8")
        manifest[aid] = update_ts
        built += 1
        print(" done")

    manifest["_version"] = ACTIVITIES_MANIFEST_VERSION
    ACTIVITIES_MANIFEST_PATH.write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    print(f"  Activity pages: {built} built, {skipped} skipped (unchanged)")


# ─────────────────────────────────────────────

def build_site():
    DIST_DIR.mkdir(exist_ok=True)
    (DIST_DIR / "style.css").write_text(
        (STATIC_DIR / "style.css").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (DIST_DIR / "theme.js").write_text(
        (STATIC_DIR / "theme.js").read_text(encoding="utf-8"), encoding="utf-8"
    )

    env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)), autoescape=True)
    env.filters["fmt_pace"]       = fmt_pace
    env.filters["fmt_duration"]   = fmt_duration
    env.filters["fmt_split_diff"] = fmt_split_diff
    env.filters["tojson_safe"]    = lambda v: Markup(json.dumps(v))

    conn = sqlite3.connect(DB_PATH)
    races = fetch_races(conn)

    print(f"\nComputing best splits for {len(races)} races...")
    all_splits = fetch_all_best_splits(conn, races)

    print(f"\nComputing pace series for {len(races)} races...")
    pace_series_data = fetch_all_pace_series(conn, races)

    print(f"\nComputing 16-week training blocks for {len(races)} races...")
    training_blocks = fetch_training_blocks(conn, races)

    print(f"\nComputing per-mile splits for {len(races)} races...")
    mile_splits_data = fetch_all_mile_splits(conn, races, pace_series_data)

    print(f"\nBuilding activity heatmap...")
    activity_heatmap = fetch_activity_heatmap(conn)

    print(f"\nComputing streak records...")
    streaks = fetch_streaks(conn)

    print(f"\nBuilding activity map data...")
    map_data = fetch_activity_map_data(conn)

    print(f"\nFetching all activities list...")
    all_activities = fetch_all_activities_list(conn)
    print(f"  {len(all_activities)} activities loaded")

    print(f"\nComputing best efforts across all activities (cached)...")
    all_effort_splits = fetch_all_best_efforts(conn, all_activities)

    print(f"\nComputing notables for all activities...")
    all_notables = compute_notables(all_activities, all_effort_splits)
    notable_counts = sum(len(v) for v in all_notables.values())
    print(f"  {notable_counts} notables across {len(all_activities)} activities")

    print(f"\nGenerating calorie strata with Ollama ({len(CALORIE_STRATA)} strata, skips cached)...")
    calorie_strata = generate_calorie_strata()

    print(f"\nBuilding per-activity pages (skips unchanged)...")
    build_activity_pages(conn, all_activities, env, all_notables, calorie_strata, all_effort_splits)

    conn.close()

    prs   = compute_prs(races)
    years = sorted({r["year"] for r in races}, reverse=True)

    # Build split lookup keyed by label for each race (for analysis template)
    splits_by_label: dict[int, dict[str, dict]] = {}
    for aid, splits in all_splits.items():
        splits_by_label[aid] = {s["label"]: s for s in splits}

    # Write JS data file separately so it never passes through Jinja2 autoescape
    js_data = {
        "pace_series": pace_series_data,
        "split_labels": [lbl for _, lbl in SPLIT_TARGETS],
    }
    (DIST_DIR / "race-data.js").write_text(
        "const RACE_DATA = " + json.dumps(js_data) + ";",
        encoding="utf-8",
    )

    shared = dict(
        races=races,
        prs=prs,
        years=years,
        all_splits=all_splits,
        splits_by_label=splits_by_label,
        split_targets=SPLIT_TARGETS,
        split_labels=[lbl for _, lbl in SPLIT_TARGETS],
        analysis_categories=ANALYSIS_CATEGORIES,
    )

    # ── races index
    tmpl = env.get_template("races.html")
    html = tmpl.render(**shared)
    (DIST_DIR / "index.html").write_text(html, encoding="utf-8")
    print(f"\nGenerated index.html ({len(races)} races)")

    # ── analysis page
    tmpl = env.get_template("analysis.html")
    html = tmpl.render(**shared)
    (DIST_DIR / "analysis.html").write_text(html, encoding="utf-8")
    print(f"Generated analysis.html")

    # ── per-race build analyses (AI)
    print(f"\nGenerating per-race build analyses with Ollama ({OLLAMA_MODEL})...")
    race_analyses = generate_race_build_analyses(training_blocks, prs, all_notables, all_effort_splits)
    for block in training_blocks:
        block["coach_notes"] = race_analyses.get(str(block["race"]["activity_id"]))

    # ── training plan
    (DIST_DIR / "training-data.js").write_text(
        "const TRAINING_DATA = " + json.dumps(training_blocks) + ";",
        encoding="utf-8",
    )
    tmpl = env.get_template("training.html")
    html = tmpl.render(**shared)
    (DIST_DIR / "training.html").write_text(html, encoding="utf-8")
    print(f"Generated training.html")

    # ── best efforts
    best_efforts = compute_best_efforts_by_distance(all_activities, all_effort_splits)
    (DIST_DIR / "best-efforts-data.js").write_text(
        "const BEST_EFFORTS_DATA = " + json.dumps(best_efforts) + ";\n"
        "const BEST_EFFORTS_LABELS = " + json.dumps([lbl for _, lbl in SPLIT_TARGETS]) + ";",
        encoding="utf-8",
    )
    tmpl = env.get_template("best-efforts.html")
    html = tmpl.render(**shared)
    (DIST_DIR / "best-efforts.html").write_text(html, encoding="utf-8")
    print(f"Generated best-efforts.html")

    # ── achievements
    achievements = compute_achievements(all_activities, all_effort_splits, streaks, activity_heatmap)
    (DIST_DIR / "achievements-data.js").write_text(
        "const ACHIEVEMENTS = " + json.dumps(achievements) + ";",
        encoding="utf-8",
    )
    tmpl = env.get_template("achievements.html")
    html = tmpl.render(**shared)
    (DIST_DIR / "achievements.html").write_text(html, encoding="utf-8")
    print(f"Generated achievements.html")

    # ── activities page
    _TIER_ORDER = {"pr": 0, "milestone": 1, "hot": 2, "good": 3, "ok": 4, "muted": 5}
    activities_log = []
    for act in all_activities:
        entry = {k: act[k] for k in _ACT_LOG_FIELDS if k in act}
        aid = act["activity_id"]
        ns_sorted = sorted(
            all_notables.get(aid, []),
            key=lambda n: _TIER_ORDER.get(n["tier"], 9),
        )
        entry["notables"] = [
            {"icon": n["icon"], "tier": n["tier"],
             "label": n["label"], "window": n["window"], "value": n.get("value", "")}
            for n in ns_sorted[:6]
        ]
        activities_log.append(entry)
    (DIST_DIR / "activities-data.js").write_text(
        "const ACTIVITIES_DATA = " + json.dumps(activity_heatmap) + ";\n"
        "const ACTIVITIES_LOG = " + json.dumps(activities_log) + ";",
        encoding="utf-8",
    )
    tmpl = env.get_template("activities.html")
    html = tmpl.render(**shared)
    (DIST_DIR / "activities.html").write_text(html, encoding="utf-8")
    print(f"Generated activities.html ({len(all_activities)} activities)")

    # ── mile splits
    (DIST_DIR / "mile-splits-data.js").write_text(
        "const MILE_SPLITS_DATA = " + json.dumps(mile_splits_data) + ";",
        encoding="utf-8",
    )
    tmpl = env.get_template("mile-splits.html")
    html = tmpl.render(**shared)
    (DIST_DIR / "mile-splits.html").write_text(html, encoding="utf-8")
    print(f"Generated mile-splits.html")

    # ── AI analysis
    ai_analysis = generate_ai_analysis(races, training_blocks, prs, all_effort_splits, all_activities)
    tmpl = env.get_template("ai-analysis.html")
    html = tmpl.render(**shared, ai=ai_analysis)
    (DIST_DIR / "ai-analysis.html").write_text(html, encoding="utf-8")
    print(f"Generated ai-analysis.html")

    # ── map page
    (DIST_DIR / "map-data.js").write_text(
        "const MAP_DATA = " + json.dumps(map_data) + ";",
        encoding="utf-8",
    )
    tmpl = env.get_template("map.html")
    html = tmpl.render(**shared)
    (DIST_DIR / "map.html").write_text(html, encoding="utf-8")
    print(f"Generated map.html")

    # ── trophy room
    trophy_data = compute_trophy_data(races, prs, all_splits)
    trophy_data["streaks"] = streaks
    (DIST_DIR / "trophy-data.js").write_text(
        "const TROPHY_DATA = " + json.dumps(trophy_data) + ";",
        encoding="utf-8",
    )
    tmpl = env.get_template("trophies.html")
    html = tmpl.render(**shared, trophy=trophy_data)
    (DIST_DIR / "trophies.html").write_text(html, encoding="utf-8")
    print(f"Generated trophies.html")


if __name__ == "__main__":
    build_site()
