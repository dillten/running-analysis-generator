# Running Analysis Generator

A Python static site generator that reads a Garmin SQLite database created by [garmin-health-data](https://github.com/diegoscarabelli/garmin-health-data) and produces a personal running analysis website. I put this together mainly as a fun project to explore my running data. There might be some sharp edges - it's not designed as a polished product, but I hope you find it useful or inspiring for your own projects!

## Prerequisites

- Python 3.x
- A Garmin SQLite database (see `DB_PATH` in `config.py`)
- [Ollama](https://ollama.com/) running locally for AI analysis features (optional)

## Quick Start

**First time only** — create a virtual environment and install dependencies:

```bash
python setup.py
```

**Every subsequent run** — build the site and serve it locally:

```bash
python run.py
```

Then open `http://localhost:5500` in your browser. Press `Ctrl+C` to stop the server.

`run.py` will automatically run setup if the virtual environment is missing, so you can also just run it directly the first time.

**Delete all built output:**

```bash
python run.py --clean
```

This removes the `dist/` directory and exits without building or serving.

### Manual usage

If you prefer not to use the scripts:

```bash
pip install jinja2 markupsafe
python generate.py
cd dist && python -m http.server 5500
```

## Pages Generated

| Page | Description |
|---|---|
| `index.html` / Activities | Full activity log with filters, heatmap, streaks, and map |
| `races.html` | Race history with pace series charts and training block analysis |
| `best-efforts.html` | Personal records at each standard distance (1K → 50K) |
| `trophies.html` / Achievements | Milestone badges and notable performances |
| `ai-analysis.html` | AI-generated career narrative and per-race build summaries |
| `activity/{id}.html` | Per-activity detail page with splits, charts, and notables earned |
| `training.html` | Week-by-week training load overview |
| `map.html` | Geographic map of all activities |
| `analysis.html` | Race performance analysis by distance category |
| `mile-splits.html` | Mile-by-mile split breakdowns |

## Caching

Four cache files are created at the project root (all gitignored) to avoid redundant computation:

| File | What it caches | How to invalidate |
|---|---|---|
| `best-efforts-cache.json` | Fastest split per distance per activity | Delete file; keyed by `activity_id → update_ts` |
| `ai-analysis-cache.json` | Overall career AI narrative | Delete file or bump `cache_version` in `generate_ai_analysis()` |
| `ai-race-analysis-cache.json` | Per-race build AI summaries | Delete file or delete individual entry |
| `ai-calorie-cache.json` / `ai-calorie-strata-cache.json` | AI food-equivalent descriptions for calorie counts | Delete files |

Per-activity HTML pages are tracked in `dist/activities-manifest.json`. Bump `ACTIVITIES_MANIFEST_VERSION` in `generate.py` to force a full rebuild of all activity pages when `templates/activity.html` changes significantly.

## AI Analysis

AI features use [Ollama](https://ollama.com/) running a local model (default: `gemma4`). No external API calls are made.

To use a different model, set `OLLAMA_MODEL` in `config.py`. To skip AI generation entirely, the site builds fine without Ollama running — cached results are used if present, and AI sections are omitted if not.

## Configuration

Copy `config.example.py` to `config.py` and edit to match your setup (`config.py` is gitignored):

| Setting | Purpose |
|---|---|
| `DB_PATH` | Path to the Garmin SQLite database |
| `OLLAMA_URL` | Ollama API endpoint (default: `http://localhost:11434`) |
| `OLLAMA_MODEL` | Local LLM model name (default: `gemma4`) |

Additional tuneable constants remain in `generate.py`:

| Constant | Purpose |
|---|---|
| `SPLIT_TARGETS` | Best-effort distances to track (1K through 50K) |
| `RUNNING_TYPES` | Activity type keys that count as running |
| `ACTIVITIES_MANIFEST_VERSION` | Bump to force rebuild of all activity pages |

## Units

- Pace displayed in **min/mile**
- Elevation displayed in **feet** (converted from metres stored in the DB)
- Distance displayed in **miles**

## Credits
[Diego Scarabelli](https://github.com/diegoscarabelli) — Garmin data extraction and database schema that made this possible. Thanks for sharing your work!

## License
> [CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/) - free to use and modify for personal, non-commercial purposes with attribution and share-alike.

## Contributions
Contributions are welcome! Please open an issue or submit a pull request with improvements, bug fixes, or new features.