# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

A Python static site generator that reads a Garmin SQLite database and produces a personal running analysis website in `dist/`. The single entry point is `generate.py` — run it to rebuild the site.

```bash
python3 generate.py
```

Or use the convenience scripts:
```bash
python run.py          # build + serve at http://localhost:5500
python run.py --clean  # delete dist/ and exit
```

## Database

Path is set by `DB_PATH` in `config.py` (default: one level up from this directory). Three key tables:

- **`activity`** — one row per activity; contains `activity_id`, `start_ts`, `distance`, `duration`, `average_speed`, `average_hr`, `activity_type_key`, `event_type_key`, `ts_data_available`, `update_ts`, `timezone_offset_hours`
- **`running_agg_metrics`** — joined 1:1 with activity; contains `elevation_gain`, `elevation_loss`, `avg_running_cadence`, `avg_power`, `vo2_max_value`, `avg_respiration_rate`, etc.
- **`activity_ts_metric`** — timeseries rows keyed by `(activity_id, name, timestamp)`; `name='distance'` gives cumulative distance in metres used for split computation

Only activities with `activity_type_key IN ('running','treadmill_running','track_running','trail_running','road_running','ultra_run')` are processed.

## Architecture

`generate.py` is the entire backend (~2500 lines). `build_site()` at the bottom orchestrates everything in order:

1. **Races pipeline** — `fetch_races()` → `fetch_all_best_splits()` → `fetch_all_pace_series()` → `fetch_training_blocks()` → `fetch_all_mile_splits()` → AI analyses
2. **All-activities pipeline** — `fetch_activity_heatmap()` → `fetch_streaks()` → `fetch_activity_map_data()` → `fetch_all_activities_list()` → `fetch_all_best_efforts()` → `compute_notables()` → `build_activity_pages()`
3. **Render** — each page calls `env.get_template(...).render(...)` and writes to `dist/`

Large JS data is written to separate `.js` files (e.g. `race-data.js`, `activities-data.js`) and loaded via `<script src>` so the data never passes through Jinja2's autoescape. Templates must follow this pattern for any sizeable JSON payload.

## Caching

Five cache files sit at the project root (all gitignored):

| File | Key | Invalidated by |
|---|---|---|
| `best-efforts-cache.json` | `activity_id → {update_ts, splits}` | DB `update_ts` change; delete to force full recompute |
| `ai-analysis-cache.json` | `{race_count, cache_version}` | Bump `cache_version` string in `generate_ai_analysis()` |
| `ai-race-analysis-cache.json` | `activity_id` | Delete entry or whole file |
| `ai-calorie-cache.json` | `activity_id` | Delete file |
| `ai-calorie-strata-cache.json` | `CALORIE_STRATA_VERSION` constant | Bump version string or delete file |

Per-activity HTML pages are tracked in `dist/activities-manifest.json` keyed by `activity_id → update_ts`. There is also a `_version` key (`ACTIVITIES_MANIFEST_VERSION` constant) — bump this string whenever `templates/activity.html` changes significantly so all pages rebuild.

## Configuration

`config.py` (gitignored; copy from `config.example.py`) holds environment-specific settings:

- **`DB_PATH`** — path to the Garmin SQLite database
- **`OLLAMA_URL`** — Ollama endpoint (default `http://localhost:11434`)
- **`OLLAMA_MODEL`** — local LLM model name (default `gemma4`)

## Key Constants (top of generate.py)

- **`SPLIT_TARGETS`** — list of `(metres, label)` tuples defining best-effort distances (1K through 50K). Add new distances here; the cache must be deleted to recompute.
- **`RUNNING_TYPES`** — tuple of `activity_type_key` values that count as running.
- **`EXCLUDED_ACTIVITY_IDS`** — set of `activity_id` ints skipped everywhere (bad GPS clock, corrupt data, etc.).
- **`ACTIVITIES_MANIFEST_VERSION`** — bump when `activity.html` template changes to force full rebuild of all 1100+ activity pages.
- **`CALORIE_STRATA`** / **`CALORIE_STRATA_VERSION`** — bucket boundaries and version key for AI-generated food-equivalent descriptions; bump version to invalidate calorie strata cache.

## Best-Effort Split Algorithm

`best_splits_for_activity()` uses a two-pointer sliding window over the distance timeseries to find the fastest contiguous segment of each `SPLIT_TARGET` length. Elapsed time comes from actual timestamp differences (not index deltas) so sparse older recordings work. A pace sanity check of 180–1800 sec/mi (3:00–30:00/mi) filters corrupt GPS teleport data. The same sanity check must be applied at consumption sites (`compute_best_efforts_by_distance`, `compute_achievements`, `compute_notables`).

## Notables System

`compute_notables()` awards per-activity badges by comparing each activity against rolling windows (all-time → 1yr → 6mo → 3mo → 1mo), awarding only the most impressive window. Metrics covered: distance, duration, elevation, training load, calories, avg HR, pace, local start hour, best split at each distance, and lifetime milestones. Notables are passed directly into Jinja2 context for `activity.html` and embedded in `ACTIVITIES_LOG` (capped at 6 per activity, sorted by tier) for the activities list page.

## Units

All elevations display in **feet** throughout. The DB stores metres; convert with `× 3.28084` at the point of formatting. Pace displays in **min/mi** as primary. The `elev_per_mi` field (ft/mi) replaced the old `elev_per_km` field — do not reintroduce metric slope units.

## Templates

Jinja2 templates in `templates/`. Custom filters registered in `build_site()`: `fmt_pace`, `fmt_duration`, `tojson_safe`. The `tojson_safe` filter outputs `Markup(json.dumps(v))` to bypass autoescape — use it only for small inline values; write large data to `.js` files instead.

Per-activity pages live at `dist/activity/{activity_id}.html` and use relative paths (`../`) to reach shared assets.

## AI Analysis

Two separate AI calls via Ollama (local LLM, no external API):
- **Overall career analysis** — `generate_ai_analysis()` using `_format_data_for_prompt()`, which includes race list, all-time best splits, volume peaks, and milestones
- **Per-race build analysis** — `generate_race_build_analyses()` using `_format_build_for_prompt()`, which includes race result, in-race GPS splits, notables earned, and week-by-week training data

Both are cached and skip if cache is valid. Delete the respective cache file to regenerate.
