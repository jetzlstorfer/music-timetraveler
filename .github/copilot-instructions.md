# Copilot Instructions

## Build & Run

```bash
pip install -r requirements.txt
python app.py              # dev server on :5000
make dev                   # Flask debug mode
```

## Testing and Linting

**Lint with Ruff:**
```bash
make lint                  # or: ruff check app.py database.py test_app.py
```

**Run tests:**
```bash
make test                  # or: pytest
pytest test_app.py::TestFirstListen::test_first_listen_returns_result   # single test
pytest -k "artist"         # run tests matching keyword
```

Tests use an `isolated_db` autouse fixture that patches `database.DB_PATH` to a temp file per test — no setup needed. External Last.fm and Spotify API calls are mocked with `unittest.mock.patch`; see the helper functions `_track_info_response`, `_library_page_html`, and `_recent_tracks_response` at the top of `test_app.py` for building fake responses. Spotify-related tests live in `TestSpotifyOAuth`, `TestSpotifyUpload`, `TestSpotifyAuthEndpoints`, `TestSpotifySync`, and `TestSpotifyFirstListen`.

## Architecture

**Flask + vanilla JS single-page app.** No frontend build step — `static/index.html` is served directly.

### Key files

- `app.py` — all Flask routes plus Last.fm scraping, Spotify OAuth/import/sync logic (~2700 lines)
- `database.py` — persistence abstraction with a unified public API (`get_cached`, `save_result`, `get_history`, plus Spotify helpers like `create_spotify_session`, `bulk_insert_spotify_plays`) backed by either SQLite (default) or Azure Cosmos DB
- `static/index.html` — the entire frontend (HTML + CSS + JS in one file)
- `infra/` — Azure Bicep templates for Container Apps deployment

### Async first-listen lookup pattern

The `/api/first-listen` endpoint returns `202` with a `lookup_id` immediately, then runs the actual lookup in a background thread. The client polls `/api/lookup-progress?lookup_id=` for status updates. Progress state lives in the in-memory `LOOKUP_PROGRESS` dict (not in the database). In tests, use the `_await_first_listen()` helper to handle this polling.

### First-listen resolution strategy (in order)

1. Check the database cache
2. Call `track.getInfo` for play count and metadata
3. Scrape the public Last.fm library page for the oldest scrobble date
4. Fall back to scanning `user.getRecentTracks` pages backward

### Database abstraction

`database.py` exposes backend-agnostic functions. Backend selection is automatic: if `COSMOS_CONNECTION_STRING` (or `COSMOS_ENDPOINT` + `COSMOS_KEY`) is set, Cosmos is used; otherwise SQLite. Both backends share identical function signatures. Text matching is case-insensitive throughout. The Cosmos deployment uses four containers: `searches` (cache + history), `spotify_profiles` (90-day TTL, refreshed via `SPOTIFY_TOUCH_INTERVAL_SECONDS`), `spotify_plays` (per-user imported listening history, written via `bulk_insert_spotify_plays` with a thread pool sized by `SPOTIFY_BULK_INSERT_WORKERS`), and `spotify_sessions` (30-day rolling TTL).

### Spotify integration

OAuth handshake at `/api/spotify/login` → `/api/spotify/callback`; identity is the Spotify user id returned by `/me`. Refresh tokens are encrypted at rest with `SPOTIFY_TOKEN_ENCRYPTION_KEY` (Fernet). The browser only holds a `spotify_session` cookie that maps to a row in the `spotify_sessions` container. Users can either upload their Spotify "Extended Streaming History" zip via `/api/spotify/upload` (filtered by `SPOTIFY_MIN_MS_PLAYED = 30_000` ms) or pull recently-played tracks via `/api/spotify/sync`. Upload progress lives in an in-memory dict polled via `/api/spotify/import-progress`, mirroring the first-listen lookup pattern.

### Artist first-listen

Track lookups *update* an existing artist first-listen cache entry if the track's date is earlier, but never *create* one — creation only happens via the dedicated `/api/artist-first-listen` endpoint which does a full artist-wide library scrape.

## Conventions

- **No framework for the frontend** — vanilla HTML/CSS/JS only, all in `static/index.html`
- **`lastfm_get()`** wraps all Last.fm API calls with retry logic (3 attempts, exponential backoff)
- **Text normalization** uses `casefold()` and whitespace collapsing (see `normalize_lastfm_text` in `app.py` and `_normalize_lookup_value` in `database.py`)
- **Test classes** group tests by feature area (e.g., `TestFirstListen`, `TestHistory`, `TestArtistFirstListenEndpoint`)
- **Environment config** via `.env` file loaded with `python-dotenv`; see `.env.example` for all variables
- **Deployment** uses Azure Developer CLI (`azd up`) targeting Azure Container Apps; CI/CD in `.github/workflows/azure-aca-deploy.yml`
