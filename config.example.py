from pathlib import Path

# Path to the Garmin SQLite database
DB_PATH = "garmin_data.db"

# Ollama local LLM settings
OLLAMA_URL   = "http://localhost:11434"
OLLAMA_MODEL = "gemma4"  # any model pulled via `ollama pull <model>`

# Upcoming races to display on the races page and generate detail pages for.
# Each entry requires: name, date (YYYY-MM-DD), distance_km, location.
# Optional: target_time (HH:MM:SS), weekly_targets (16 miles values, week 16→race week).
FUTURE_RACES = [
    # {
    #     "name": "My Target Race",
    #     "date": "2026-10-01",
    #     "distance_km": 42.195,
    #     "location": "City, Country",
    #     "target_time": "3:30:00",
    #     "weekly_targets": [30, 32, 35, 38, 40, 42, 40, 44, 42, 38, 35, 32, 28, 22, 14, 8],
    # },
]
