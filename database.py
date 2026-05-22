"""Persistence layer for Last.fm Time Traveler.

The app supports two storage backends:

- SQLite for local and test workflows.
- Azure Cosmos DB for NoSQL when Cosmos environment variables are configured.

The public API remains the same so the rest of the Flask app can treat the
cache as a simple key-value store with history queries by username.
"""

import hashlib
import os
import re
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta

try:
    from azure.cosmos import CosmosClient, PartitionKey, exceptions as cosmos_exceptions
except ImportError:
    CosmosClient = None
    PartitionKey = None
    cosmos_exceptions = None


DEFAULT_SQLITE_DB_PATH = "timetraveler.db"
DB_PATH = os.getenv("DB_PATH", DEFAULT_SQLITE_DB_PATH)
DEFAULT_COSMOS_DATABASE_NAME = "lastfm-timetraveler"
DEFAULT_COSMOS_CONTAINER_NAME = "searches"
COSMOS_SPOTIFY_PROFILES_CONTAINER = "spotify_profiles"
COSMOS_SPOTIFY_PLAYS_CONTAINER = "spotify_plays"
COSMOS_SPOTIFY_SESSIONS_CONTAINER = "spotify_sessions"
SPOTIFY_BULK_INSERT_WORKERS = int(os.getenv("SPOTIFY_BULK_INSERT_WORKERS", "16"))
# How often (seconds) to refresh a profile's TTL on access. The Cosmos
# container has a 90-day TTL; touching less often than once a day would risk
# expiry, more often is wasted writes.
SPOTIFY_TOUCH_INTERVAL_SECONDS = int(os.getenv("SPOTIFY_TOUCH_INTERVAL_SECONDS", "3600"))
# Sessions get a 30-day rolling TTL refreshed on every authenticated access.
SPOTIFY_SESSION_TTL_SECONDS = int(os.getenv("SPOTIFY_SESSION_TTL_SECONDS", str(30 * 24 * 60 * 60)))
SQLITE_TIMEOUT_SECONDS = 30
INIT_DB_MAX_ATTEMPTS = 3
INIT_DB_RETRY_DELAY_SECONDS = 0.25
_SQLITE_INIT_LOCK = threading.Lock()
_INITIALIZED_SQLITE_DB_PATH = None
_COSMOS_INIT_LOCK = threading.Lock()
_COSMOS_SIGNATURE = None
_COSMOS_CLIENT = None
_COSMOS_DATABASE = None
_COSMOS_CONTAINER = None
_COSMOS_EXTRA_CONTAINERS: dict[str, object] = {}


def _normalize_lookup_value(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip().casefold()


def _cache_item_id(username: str, track: str, artist: str) -> str:
    return "|".join(
        [
            _normalize_lookup_value(username),
            _normalize_lookup_value(artist),
            _normalize_lookup_value(track),
        ]
    )


def _artist_first_listen_item_id(username: str, artist: str) -> str:
    return "artist_first_listen|" + "|".join(
        [
            _normalize_lookup_value(username),
            _normalize_lookup_value(artist),
        ]
    )


def _use_cosmos_backend() -> bool:
    return bool(_cosmos_connection_string() or (_cosmos_endpoint() and _cosmos_key()))


def _sqlite_db_path() -> str:
    if DB_PATH != DEFAULT_SQLITE_DB_PATH:
        return DB_PATH
    return os.getenv("DB_PATH", DEFAULT_SQLITE_DB_PATH)


def _cosmos_connection_string() -> str:
    return os.getenv("COSMOS_CONNECTION_STRING", "").strip()


def _cosmos_endpoint() -> str:
    return os.getenv("COSMOS_ENDPOINT", "").strip()


def _cosmos_key() -> str:
    return os.getenv("COSMOS_KEY", "").strip()


def _cosmos_database_name() -> str:
    return os.getenv("COSMOS_DATABASE_NAME", DEFAULT_COSMOS_DATABASE_NAME).strip() or DEFAULT_COSMOS_DATABASE_NAME


def _cosmos_container_name() -> str:
    return os.getenv("COSMOS_CONTAINER_NAME", DEFAULT_COSMOS_CONTAINER_NAME).strip() or DEFAULT_COSMOS_CONTAINER_NAME


def _sqlite_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_sqlite_db_path(), timeout=SQLITE_TIMEOUT_SECONDS)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {SQLITE_TIMEOUT_SECONDS * 1000}")
    return conn


def _is_locked_error(exc: sqlite3.OperationalError) -> bool:
    return "database is locked" in str(exc).lower()


def _create_sqlite_schema(conn: sqlite3.Connection) -> None:
    # Lightweight migration: the Spotify-OAuth rework changed the schema of
    # spotify_profiles (token_hash -> refresh_token_encrypted) and added
    # spotify_sessions. Older local DBs created before the rework still have
    # the old shape, so drop the legacy spotify_* tables before re-creating
    # them. (No production data exists for this branch — we already wiped
    # the schema per the migration plan.)
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(spotify_profiles)").fetchall()]
        if cols and "refresh_token_encrypted" not in cols:
            conn.execute("DROP TABLE IF EXISTS spotify_profiles")
            conn.execute("DROP TABLE IF EXISTS spotify_sessions")
            conn.execute("DROP TABLE IF EXISTS spotify_history")
    except sqlite3.DatabaseError:
        # Brand-new DB or unreadable PRAGMA — nothing to migrate.
        pass

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS searches (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            username                TEXT    NOT NULL,
            track                   TEXT    NOT NULL,
            artist                  TEXT    NOT NULL,
            album                   TEXT,
            first_listen_date       TEXT,
            first_listen_timestamp  TEXT,
            total_scrobbles         INTEGER,
            image                   TEXT,
            queried_at              TEXT    NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_searches_user_track_artist
        ON searches (LOWER(username), LOWER(track), LOWER(artist))
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS artist_first_listens (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            username                TEXT    NOT NULL,
            artist                  TEXT    NOT NULL,
            first_listen_track      TEXT,
            first_listen_date       TEXT,
            first_listen_timestamp  TEXT,
            queried_at              TEXT    NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_artist_first_listens_user_artist
        ON artist_first_listens (LOWER(username), LOWER(artist))
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS spotify_profiles (
            profile_id              TEXT PRIMARY KEY,
            display_name            TEXT,
            avatar_url              TEXT,
            refresh_token_encrypted TEXT,
            scopes                  TEXT,
            created_at              TEXT NOT NULL,
            last_login_at           TEXT,
            last_sync_at            TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS spotify_sessions (
            session_id_hash TEXT PRIMARY KEY,
            profile_id      TEXT NOT NULL,
            created_at      TEXT NOT NULL,
            last_used_at    TEXT NOT NULL,
            expires_at      TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_spotify_sessions_profile
        ON spotify_sessions (LOWER(profile_id))
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS spotify_history (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_id      TEXT    NOT NULL,
            track           TEXT    NOT NULL,
            artist          TEXT    NOT NULL,
            album           TEXT,
            played_at       TEXT    NOT NULL,
            played_at_unix  INTEGER NOT NULL,
            ms_played       INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_spotify_history_dedup
        ON spotify_history (LOWER(profile_id), played_at, LOWER(track), LOWER(artist))
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_spotify_history_track_artist
        ON spotify_history (LOWER(profile_id), LOWER(track), LOWER(artist))
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_spotify_history_artist
        ON spotify_history (LOWER(profile_id), LOWER(artist))
        """
    )
    conn.commit()


def _get_cosmos_container():
    global _COSMOS_SIGNATURE, _COSMOS_CLIENT, _COSMOS_DATABASE, _COSMOS_CONTAINER

    if CosmosClient is None or PartitionKey is None or cosmos_exceptions is None:
        raise RuntimeError(
            "azure-cosmos is not installed. Add the package and configure Cosmos DB environment variables."
        )

    connection_string = _cosmos_connection_string()
    endpoint = _cosmos_endpoint()
    key = _cosmos_key()
    database_name = _cosmos_database_name()
    container_name = _cosmos_container_name()
    signature = (connection_string, endpoint, key, database_name, container_name)

    with _COSMOS_INIT_LOCK:
        if _COSMOS_CONTAINER is not None and _COSMOS_SIGNATURE == signature:
            return _COSMOS_CONTAINER

        if connection_string:
            client = CosmosClient.from_connection_string(connection_string)
        elif endpoint and key:
            client = CosmosClient(endpoint, credential=key)
        else:
            raise RuntimeError(
                "Cosmos DB is selected but no credentials were provided. Set COSMOS_CONNECTION_STRING or COSMOS_ENDPOINT and COSMOS_KEY."
            )

        database = client.create_database_if_not_exists(id=database_name)
        container = database.create_container_if_not_exists(
            id=container_name,
            partition_key=PartitionKey(path="/username_normalized"),
        )

        _COSMOS_SIGNATURE = signature
        _COSMOS_CLIENT = client
        _COSMOS_DATABASE = database
        _COSMOS_CONTAINER = container
        return container


def _get_cosmos_named_container(container_name: str, partition_key_path: str):
    """Return (and lazily create) a Cosmos container alongside the primary one."""
    # Ensure the primary client/database is initialized first.
    _get_cosmos_container()
    with _COSMOS_INIT_LOCK:
        cached = _COSMOS_EXTRA_CONTAINERS.get(container_name)
        if cached is not None:
            return cached
        if _COSMOS_DATABASE is None:
            raise RuntimeError("Cosmos database is not initialized")
        container = _COSMOS_DATABASE.create_container_if_not_exists(
            id=container_name,
            partition_key=PartitionKey(path=partition_key_path),
        )
        _COSMOS_EXTRA_CONTAINERS[container_name] = container
        return container


def _spotify_profiles_container():
    return _get_cosmos_named_container(COSMOS_SPOTIFY_PROFILES_CONTAINER, "/profile_id_normalized")


def _spotify_plays_container():
    return _get_cosmos_named_container(COSMOS_SPOTIFY_PLAYS_CONTAINER, "/profile_id_normalized")


def _spotify_sessions_container():
    return _get_cosmos_named_container(COSMOS_SPOTIFY_SESSIONS_CONTAINER, "/session_id_hash")


def _spotify_play_doc_id(profile_id: str, played_at: str, track: str, artist: str) -> str:
    """Deterministic doc id matches the SQLite dedup index semantics."""
    raw = "|".join([
        _normalize_lookup_value(profile_id),
        played_at or "",
        _normalize_lookup_value(track),
        _normalize_lookup_value(artist),
    ])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _spotify_profile_doc(
    profile_id: str,
    *,
    display_name: str = "",
    avatar_url: str = "",
    refresh_token_encrypted: str = "",
    scopes: str = "",
) -> dict:
    now_iso = datetime.now(timezone.utc).isoformat()
    return {
        "id": _normalize_lookup_value(profile_id),
        "type": "spotify_profile",
        "profile_id": profile_id,
        "profile_id_normalized": _normalize_lookup_value(profile_id),
        "display_name": display_name or "",
        "avatar_url": avatar_url or "",
        "refresh_token_encrypted": refresh_token_encrypted or "",
        "scopes": scopes or "",
        "created_at": now_iso,
        "last_login_at": now_iso,
        "last_sync_at": "",
    }


def _spotify_play_doc(profile_id: str, play: dict) -> dict:
    track = play["track"]
    artist = play["artist"]
    played_at = play["played_at"]
    return {
        "id": _spotify_play_doc_id(profile_id, played_at, track, artist),
        "type": "spotify_play",
        "profile_id": profile_id,
        "profile_id_normalized": _normalize_lookup_value(profile_id),
        "track": track,
        "track_normalized": _normalize_lookup_value(track),
        "artist": artist,
        "artist_normalized": _normalize_lookup_value(artist),
        "album": play.get("album", "") or "",
        "played_at": played_at,
        "played_at_unix": int(play["played_at_unix"]),
        "ms_played": int(play.get("ms_played", 0) or 0),
    }


def _cosmos_item(
    username: str,
    track: str,
    artist: str,
    album: str,
    first_listen_date: str,
    first_listen_timestamp: str,
    total_scrobbles: int,
    image: str,
) -> dict:
    return {
        "id": _cache_item_id(username, track, artist),
        "type": "search",
        "username": username,
        "username_normalized": _normalize_lookup_value(username),
        "track": track,
        "track_normalized": _normalize_lookup_value(track),
        "artist": artist,
        "artist_normalized": _normalize_lookup_value(artist),
        "album": album,
        "first_listen_date": first_listen_date,
        "first_listen_timestamp": first_listen_timestamp,
        "total_scrobbles": total_scrobbles,
        "image": image,
        "queried_at": datetime.now(timezone.utc).isoformat(),
    }


def _record_from_cosmos_item(item: dict | None) -> dict | None:
    if not item:
        return None
    return {
        "id": item.get("id"),
        "username": item.get("username", ""),
        "track": item.get("track", ""),
        "artist": item.get("artist", ""),
        "album": item.get("album", ""),
        "first_listen_date": item.get("first_listen_date", ""),
        "first_listen_timestamp": item.get("first_listen_timestamp", ""),
        "total_scrobbles": item.get("total_scrobbles", 0),
        "image": item.get("image", ""),
        "queried_at": item.get("queried_at", ""),
    }


def _cosmos_artist_first_listen_item(
    username: str,
    artist: str,
    first_listen_track: str,
    first_listen_date: str,
    first_listen_timestamp: str,
) -> dict:
    return {
        "id": _artist_first_listen_item_id(username, artist),
        "type": "artist_first_listen",
        "username": username,
        "username_normalized": _normalize_lookup_value(username),
        "artist": artist,
        "artist_normalized": _normalize_lookup_value(artist),
        "first_listen_track": first_listen_track,
        "first_listen_date": first_listen_date,
        "first_listen_timestamp": first_listen_timestamp,
        "queried_at": datetime.now(timezone.utc).isoformat(),
    }


def _artist_first_listen_record_from_cosmos_item(item: dict | None) -> dict | None:
    if not item:
        return None
    return {
        "id": item.get("id"),
        "username": item.get("username", ""),
        "artist": item.get("artist", ""),
        "first_listen_track": item.get("first_listen_track", ""),
        "first_listen_date": item.get("first_listen_date", ""),
        "first_listen_timestamp": item.get("first_listen_timestamp", ""),
        "queried_at": item.get("queried_at", ""),
    }


def _sqlite_init_db() -> None:
    global _INITIALIZED_SQLITE_DB_PATH

    current_db_path = _sqlite_db_path()
    if _INITIALIZED_SQLITE_DB_PATH == current_db_path:
        return

    with _SQLITE_INIT_LOCK:
        if _INITIALIZED_SQLITE_DB_PATH == current_db_path:
            return

        for attempt in range(INIT_DB_MAX_ATTEMPTS):
            try:
                with _sqlite_connect() as conn:
                    _create_sqlite_schema(conn)
                _INITIALIZED_SQLITE_DB_PATH = current_db_path
                return
            except sqlite3.OperationalError as exc:
                if not _is_locked_error(exc) or attempt == INIT_DB_MAX_ATTEMPTS - 1:
                    raise
                time.sleep(INIT_DB_RETRY_DELAY_SECONDS)


def _sqlite_get_cached(username: str, track: str, artist: str) -> dict | None:
    _sqlite_init_db()
    with _sqlite_connect() as conn:
        row = conn.execute(
            """
            SELECT * FROM searches
            WHERE LOWER(username) = LOWER(?)
              AND LOWER(track)    = LOWER(?)
              AND LOWER(artist)   = LOWER(?)
            """,
            (username, track, artist),
        ).fetchone()
    return dict(row) if row else None


def _sqlite_save_result(
    username: str,
    track: str,
    artist: str,
    album: str,
    first_listen_date: str,
    first_listen_timestamp: str,
    total_scrobbles: int,
    image: str,
) -> None:
    _sqlite_init_db()
    queried_at = datetime.now(timezone.utc).isoformat()
    existing = _sqlite_get_cached(username, track, artist)
    with _sqlite_connect() as conn:
        if existing:
            conn.execute(
                """
                UPDATE searches
                SET album                  = ?,
                    first_listen_date      = ?,
                    first_listen_timestamp = ?,
                    total_scrobbles        = ?,
                    image                  = ?,
                    queried_at             = ?
                WHERE id = ?
                """,
                (
                    album,
                    first_listen_date,
                    first_listen_timestamp,
                    total_scrobbles,
                    image,
                    queried_at,
                    existing["id"],
                ),
            )
        else:
            conn.execute(
                """
                INSERT INTO searches
                    (username, track, artist, album, first_listen_date,
                     first_listen_timestamp, total_scrobbles, image, queried_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    username,
                    track,
                    artist,
                    album,
                    first_listen_date,
                    first_listen_timestamp,
                    total_scrobbles,
                    image,
                    queried_at,
                ),
            )
        conn.commit()


def _sqlite_get_history(username: str) -> list[dict]:
    _sqlite_init_db()
    with _sqlite_connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM searches
            WHERE LOWER(username) = LOWER(?)
            ORDER BY queried_at DESC, id DESC
            """,
            (username,),
        ).fetchall()
    return [dict(r) for r in rows]


def _sqlite_get_artist_first_listen(username: str, artist: str) -> dict | None:
    _sqlite_init_db()
    with _sqlite_connect() as conn:
        row = conn.execute(
            """
            SELECT * FROM artist_first_listens
            WHERE LOWER(username) = LOWER(?)
              AND LOWER(artist)   = LOWER(?)
            """,
            (username, artist),
        ).fetchone()
    return dict(row) if row else None


def _sqlite_save_artist_first_listen(
    username: str,
    artist: str,
    first_listen_track: str,
    first_listen_date: str,
    first_listen_timestamp: str,
) -> None:
    _sqlite_init_db()
    queried_at = datetime.now(timezone.utc).isoformat()
    existing = _sqlite_get_artist_first_listen(username, artist)
    with _sqlite_connect() as conn:
        if existing:
            conn.execute(
                """
                UPDATE artist_first_listens
                SET first_listen_track     = ?,
                    first_listen_date      = ?,
                    first_listen_timestamp = ?,
                    queried_at             = ?
                WHERE id = ?
                """,
                (
                    first_listen_track,
                    first_listen_date,
                    first_listen_timestamp,
                    queried_at,
                    existing["id"],
                ),
            )
        else:
            conn.execute(
                """
                INSERT INTO artist_first_listens
                    (username, artist, first_listen_track,
                     first_listen_date, first_listen_timestamp, queried_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    username,
                    artist,
                    first_listen_track,
                    first_listen_date,
                    first_listen_timestamp,
                    queried_at,
                ),
            )
        conn.commit()


def _cosmos_init_db() -> None:
    _get_cosmos_container()


def _cosmos_get_cached(username: str, track: str, artist: str) -> dict | None:
    container = _get_cosmos_container()
    item_id = _cache_item_id(username, track, artist)
    partition_key = _normalize_lookup_value(username)
    try:
        item = container.read_item(item=item_id, partition_key=partition_key)
    except cosmos_exceptions.CosmosResourceNotFoundError:
        return None
    return _record_from_cosmos_item(item)


def _cosmos_save_result(
    username: str,
    track: str,
    artist: str,
    album: str,
    first_listen_date: str,
    first_listen_timestamp: str,
    total_scrobbles: int,
    image: str,
) -> None:
    container = _get_cosmos_container()
    container.upsert_item(
        _cosmos_item(
            username=username,
            track=track,
            artist=artist,
            album=album,
            first_listen_date=first_listen_date,
            first_listen_timestamp=first_listen_timestamp,
            total_scrobbles=total_scrobbles,
            image=image,
        )
    )


def _cosmos_get_history(username: str) -> list[dict]:
    container = _get_cosmos_container()
    query = (
        "SELECT * FROM c WHERE c.username_normalized = @username "
        "AND c.type = 'search' "
        "ORDER BY c.queried_at DESC"
    )
    items = container.query_items(
        query=query,
        parameters=[{"name": "@username", "value": _normalize_lookup_value(username)}],
        enable_cross_partition_query=False,
    )
    return [_record_from_cosmos_item(item) for item in items]


def _cosmos_get_artist_first_listen(username: str, artist: str) -> dict | None:
    container = _get_cosmos_container()
    item_id = _artist_first_listen_item_id(username, artist)
    partition_key = _normalize_lookup_value(username)
    try:
        item = container.read_item(item=item_id, partition_key=partition_key)
    except cosmos_exceptions.CosmosResourceNotFoundError:
        return None
    return _artist_first_listen_record_from_cosmos_item(item)


def _cosmos_save_artist_first_listen(
    username: str,
    artist: str,
    first_listen_track: str,
    first_listen_date: str,
    first_listen_timestamp: str,
) -> None:
    container = _get_cosmos_container()
    container.upsert_item(
        _cosmos_artist_first_listen_item(
            username=username,
            artist=artist,
            first_listen_track=first_listen_track,
            first_listen_date=first_listen_date,
            first_listen_timestamp=first_listen_timestamp,
        )
    )


def init_db() -> None:
    """Initialize the configured persistence backend."""
    if _use_cosmos_backend():
        _cosmos_init_db()
        return
    _sqlite_init_db()


def get_cached(username: str, track: str, artist: str) -> dict | None:
    """Return the stored result for *(username, track, artist)*, or ``None``."""
    if _use_cosmos_backend():
        return _cosmos_get_cached(username, track, artist)
    return _sqlite_get_cached(username, track, artist)


def save_result(
    username: str,
    track: str,
    artist: str,
    album: str,
    first_listen_date: str,
    first_listen_timestamp: str,
    total_scrobbles: int,
    image: str,
) -> None:
    """Insert or update a first-listen result in the configured backend."""
    if _use_cosmos_backend():
        _cosmos_save_result(
            username=username,
            track=track,
            artist=artist,
            album=album,
            first_listen_date=first_listen_date,
            first_listen_timestamp=first_listen_timestamp,
            total_scrobbles=total_scrobbles,
            image=image,
        )
        return
    _sqlite_save_result(
        username=username,
        track=track,
        artist=artist,
        album=album,
        first_listen_date=first_listen_date,
        first_listen_timestamp=first_listen_timestamp,
        total_scrobbles=total_scrobbles,
        image=image,
    )


def get_history(username: str) -> list[dict]:
    """Return all stored searches for *username*, newest first."""
    if _use_cosmos_backend():
        return _cosmos_get_history(username)
    return _sqlite_get_history(username)


def clear_lastfm_data(username: str) -> dict:
    """Delete all Last.fm rows for *username* and return deletion counts."""
    normalized = _normalize_lookup_value(username)
    if not normalized:
        return {"searches": 0, "artist_first_listens": 0, "total": 0}

    if _use_cosmos_backend():
        container = _get_cosmos_container()
        ids = list(container.query_items(
            query=(
                "SELECT c.id, c.type FROM c WHERE c.username_normalized = @u "
                "AND (c.type = 'search' OR c.type = 'artist_first_listen')"
            ),
            parameters=[{"name": "@u", "value": normalized}],
            partition_key=normalized,
        ))
        searches = 0
        artist_first_listens = 0
        for item in ids:
            item_type = item.get("type", "")
            if item_type == "search":
                searches += 1
            elif item_type == "artist_first_listen":
                artist_first_listens += 1
            try:
                container.delete_item(item=item["id"], partition_key=normalized)
            except cosmos_exceptions.CosmosResourceNotFoundError:
                pass
        total = searches + artist_first_listens
        return {
            "searches": searches,
            "artist_first_listens": artist_first_listens,
            "total": total,
        }

    _sqlite_init_db()
    with _sqlite_connect() as conn:
        searches_cur = conn.execute(
            "DELETE FROM searches WHERE LOWER(username) = LOWER(?)",
            (username,),
        )
        artist_cur = conn.execute(
            "DELETE FROM artist_first_listens WHERE LOWER(username) = LOWER(?)",
            (username,),
        )
        conn.commit()
        searches = searches_cur.rowcount or 0
        artist_first_listens = artist_cur.rowcount or 0
        total = searches + artist_first_listens
        return {
            "searches": searches,
            "artist_first_listens": artist_first_listens,
            "total": total,
        }


def get_artist_first_listen(username: str, artist: str) -> dict | None:
    """Return the stored artist first-listen record for *(username, artist)*, or ``None``."""
    if _use_cosmos_backend():
        return _cosmos_get_artist_first_listen(username, artist)
    return _sqlite_get_artist_first_listen(username, artist)


def save_artist_first_listen(
    username: str,
    artist: str,
    first_listen_track: str,
    first_listen_date: str,
    first_listen_timestamp: str,
) -> None:
    """Insert or update an artist first-listen record in the configured backend."""
    if _use_cosmos_backend():
        _cosmos_save_artist_first_listen(
            username=username,
            artist=artist,
            first_listen_track=first_listen_track,
            first_listen_date=first_listen_date,
            first_listen_timestamp=first_listen_timestamp,
        )
        return
    _sqlite_save_artist_first_listen(
        username=username,
        artist=artist,
        first_listen_track=first_listen_track,
        first_listen_date=first_listen_date,
        first_listen_timestamp=first_listen_timestamp,
    )


# ---------------------------------------------------------------------------
# Spotify Extended Streaming History
#
# Storage: SQLite by default; Azure Cosmos DB when configured (see
# `_use_cosmos_backend`). Cosmos uses two containers:
#   - `spotify_profiles`  (partition key: /profile_id_normalized)
#   - `spotify_plays`     (partition key: /profile_id_normalized)
# Play documents have a deterministic SHA-1 `id` (see `_spotify_play_doc_id`)
# so re-uploads are naturally deduplicated via 409 Conflict.
# ---------------------------------------------------------------------------


def upsert_spotify_profile(
    profile_id: str,
    *,
    display_name: str = "",
    avatar_url: str = "",
    refresh_token_encrypted: str = "",
    scopes: str = "",
) -> None:
    """Create or update a Spotify profile keyed by Spotify user ID.

    Existing rows have their `display_name`, `avatar_url`, `scopes` and
    (if non-empty) `refresh_token_encrypted` refreshed; `created_at` is
    preserved; `last_login_at` is bumped to now.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    if _use_cosmos_backend():
        container = _spotify_profiles_container()
        normalized = _normalize_lookup_value(profile_id)
        try:
            existing = container.read_item(item=normalized, partition_key=normalized)
        except cosmos_exceptions.CosmosResourceNotFoundError:
            existing = None
        if existing is None:
            doc = _spotify_profile_doc(
                profile_id,
                display_name=display_name,
                avatar_url=avatar_url,
                refresh_token_encrypted=refresh_token_encrypted,
                scopes=scopes,
            )
        else:
            doc = existing
            doc["display_name"] = display_name or doc.get("display_name", "")
            doc["avatar_url"] = avatar_url or doc.get("avatar_url", "")
            if scopes:
                doc["scopes"] = scopes
            if refresh_token_encrypted:
                doc["refresh_token_encrypted"] = refresh_token_encrypted
            doc["last_login_at"] = now_iso
        container.upsert_item(body=doc)
        return

    _sqlite_init_db()
    with _sqlite_connect() as conn:
        row = conn.execute(
            "SELECT refresh_token_encrypted FROM spotify_profiles WHERE LOWER(profile_id) = LOWER(?)",
            (profile_id,),
        ).fetchone()
        if row is None:
            conn.execute(
                """
                INSERT INTO spotify_profiles
                    (profile_id, display_name, avatar_url, refresh_token_encrypted,
                     scopes, created_at, last_login_at, last_sync_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    profile_id,
                    display_name or "",
                    avatar_url or "",
                    refresh_token_encrypted or "",
                    scopes or "",
                    now_iso,
                    now_iso,
                    "",
                ),
            )
        else:
            conn.execute(
                """
                UPDATE spotify_profiles
                SET display_name            = COALESCE(NULLIF(?, ''), display_name),
                    avatar_url              = COALESCE(NULLIF(?, ''), avatar_url),
                    scopes                  = COALESCE(NULLIF(?, ''), scopes),
                    refresh_token_encrypted = COALESCE(NULLIF(?, ''), refresh_token_encrypted),
                    last_login_at           = ?
                WHERE LOWER(profile_id) = LOWER(?)
                """,
                (display_name or "", avatar_url or "", scopes or "",
                 refresh_token_encrypted or "", now_iso, profile_id),
            )
        conn.commit()


def get_spotify_profile(profile_id: str) -> dict | None:
    """Return the stored profile document, or None."""
    if _use_cosmos_backend():
        container = _spotify_profiles_container()
        normalized = _normalize_lookup_value(profile_id)
        try:
            return container.read_item(item=normalized, partition_key=normalized)
        except cosmos_exceptions.CosmosResourceNotFoundError:
            return None

    _sqlite_init_db()
    with _sqlite_connect() as conn:
        row = conn.execute(
            "SELECT * FROM spotify_profiles WHERE LOWER(profile_id) = LOWER(?)",
            (profile_id,),
        ).fetchone()
    return dict(row) if row else None


def update_spotify_refresh_token(profile_id: str, refresh_token_encrypted: str) -> None:
    """Persist a rotated refresh token."""
    if not refresh_token_encrypted:
        return
    if _use_cosmos_backend():
        container = _spotify_profiles_container()
        normalized = _normalize_lookup_value(profile_id)
        try:
            doc = container.read_item(item=normalized, partition_key=normalized)
        except cosmos_exceptions.CosmosResourceNotFoundError:
            return
        doc["refresh_token_encrypted"] = refresh_token_encrypted
        container.upsert_item(body=doc)
        return
    _sqlite_init_db()
    with _sqlite_connect() as conn:
        conn.execute(
            "UPDATE spotify_profiles SET refresh_token_encrypted = ? WHERE LOWER(profile_id) = LOWER(?)",
            (refresh_token_encrypted, profile_id),
        )
        conn.commit()


def update_spotify_last_sync(profile_id: str, when_iso: str | None = None) -> None:
    when_iso = when_iso or datetime.now(timezone.utc).isoformat()
    if _use_cosmos_backend():
        container = _spotify_profiles_container()
        normalized = _normalize_lookup_value(profile_id)
        try:
            doc = container.read_item(item=normalized, partition_key=normalized)
        except cosmos_exceptions.CosmosResourceNotFoundError:
            return
        doc["last_sync_at"] = when_iso
        container.upsert_item(body=doc)
        return
    _sqlite_init_db()
    with _sqlite_connect() as conn:
        conn.execute(
            "UPDATE spotify_profiles SET last_sync_at = ? WHERE LOWER(profile_id) = LOWER(?)",
            (when_iso, profile_id),
        )
        conn.commit()


def spotify_profile_exists(profile_id: str) -> bool:
    if _use_cosmos_backend():
        container = _spotify_profiles_container()
        normalized = _normalize_lookup_value(profile_id)
        try:
            container.read_item(item=normalized, partition_key=normalized)
            return True
        except cosmos_exceptions.CosmosResourceNotFoundError:
            return False

    _sqlite_init_db()
    with _sqlite_connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM spotify_profiles WHERE LOWER(profile_id) = LOWER(?)",
            (profile_id,),
        ).fetchone()
    return row is not None


# ---- Sessions --------------------------------------------------------------
# Sessions are server-side rows. The browser cookie carries only an opaque
# random id whose SHA-256 hash is the document key; raw session ids are never
# stored, so a DB leak alone cannot impersonate a user.

def _hash_session_id(session_id: str) -> str:
    return hashlib.sha256((session_id or "").encode("utf-8")).hexdigest()


def create_spotify_session(profile_id: str, session_id: str) -> None:
    """Store a new session keyed by SHA-256(session_id)."""
    now = datetime.now(timezone.utc)
    expires_dt = now + timedelta(seconds=SPOTIFY_SESSION_TTL_SECONDS)
    sid_hash = _hash_session_id(session_id)
    doc = {
        "id": sid_hash,
        "session_id_hash": sid_hash,
        "profile_id": profile_id,
        "created_at": now.isoformat(),
        "last_used_at": now.isoformat(),
        "expires_at": expires_dt.isoformat(),
    }
    if _use_cosmos_backend():
        container = _spotify_sessions_container()
        container.upsert_item(body=doc)
        return
    _sqlite_init_db()
    with _sqlite_connect() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO spotify_sessions
                (session_id_hash, profile_id, created_at, last_used_at, expires_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (sid_hash, profile_id, doc["created_at"], doc["last_used_at"], doc["expires_at"]),
        )
        conn.commit()


def verify_spotify_session(session_id: str) -> str | None:
    """Return the profile_id (Spotify user id) for a valid session, or None.

    Touches `last_used_at` and extends `expires_at` so active sessions don't
    age out. Throttled by SPOTIFY_TOUCH_INTERVAL_SECONDS to limit writes.
    """
    if not session_id:
        return None
    sid_hash = _hash_session_id(session_id)
    now = datetime.now(timezone.utc)
    if _use_cosmos_backend():
        container = _spotify_sessions_container()
        try:
            doc = container.read_item(item=sid_hash, partition_key=sid_hash)
        except cosmos_exceptions.CosmosResourceNotFoundError:
            return None
        # Honor explicit expiry even if the Cosmos TTL hasn't reaped the doc yet.
        expires_at = doc.get("expires_at") or ""
        if expires_at:
            try:
                exp_dt = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
                if exp_dt < now:
                    return None
            except ValueError:
                pass
        last_used = doc.get("last_used_at") or doc.get("created_at") or ""
        should_touch = True
        if last_used:
            try:
                last_dt = datetime.fromisoformat(last_used.replace("Z", "+00:00"))
                if (now - last_dt).total_seconds() < SPOTIFY_TOUCH_INTERVAL_SECONDS:
                    should_touch = False
            except ValueError:
                pass
        if should_touch:
            doc["last_used_at"] = now.isoformat()
            doc["expires_at"] = (now + timedelta(seconds=SPOTIFY_SESSION_TTL_SECONDS)).isoformat()
            try:
                container.upsert_item(body=doc)
            except Exception:  # noqa: BLE001
                pass
        return doc.get("profile_id") or None

    _sqlite_init_db()
    with _sqlite_connect() as conn:
        row = conn.execute(
            "SELECT profile_id, expires_at, last_used_at FROM spotify_sessions WHERE session_id_hash = ?",
            (sid_hash,),
        ).fetchone()
        if not row:
            return None
        try:
            exp_dt = datetime.fromisoformat((row["expires_at"] or "").replace("Z", "+00:00"))
            if exp_dt < now:
                return None
        except ValueError:
            return None
        new_exp = (now + timedelta(seconds=SPOTIFY_SESSION_TTL_SECONDS)).isoformat()
        conn.execute(
            "UPDATE spotify_sessions SET last_used_at = ?, expires_at = ? WHERE session_id_hash = ?",
            (now.isoformat(), new_exp, sid_hash),
        )
        conn.commit()
        return row["profile_id"]


def delete_spotify_session(session_id: str) -> None:
    if not session_id:
        return
    sid_hash = _hash_session_id(session_id)
    if _use_cosmos_backend():
        container = _spotify_sessions_container()
        try:
            container.delete_item(item=sid_hash, partition_key=sid_hash)
        except cosmos_exceptions.CosmosResourceNotFoundError:
            pass
        return
    _sqlite_init_db()
    with _sqlite_connect() as conn:
        conn.execute("DELETE FROM spotify_sessions WHERE session_id_hash = ?", (sid_hash,))
        conn.commit()


def delete_all_spotify_sessions(profile_id: str) -> None:
    """Revoke every session for *profile_id* (used during account-clear / logout-everywhere)."""
    if _use_cosmos_backend():
        container = _spotify_sessions_container()
        items = list(container.query_items(
            query="SELECT c.id, c.session_id_hash FROM c WHERE c.profile_id = @p",
            parameters=[{"name": "@p", "value": profile_id}],
            enable_cross_partition_query=True,
        ))
        for it in items:
            try:
                container.delete_item(item=it["id"], partition_key=it["session_id_hash"])
            except cosmos_exceptions.CosmosResourceNotFoundError:
                pass
        return
    _sqlite_init_db()
    with _sqlite_connect() as conn:
        conn.execute(
            "DELETE FROM spotify_sessions WHERE LOWER(profile_id) = LOWER(?)",
            (profile_id,),
        )
        conn.commit()


def save_spotify_plays(profile_id: str, plays: list[dict]) -> int:
    """Bulk-insert Spotify plays. Returns the number of newly-inserted rows.

    Each *play* dict must have keys: track, artist, album, played_at,
    played_at_unix, ms_played. Existing rows are ignored (deterministic id).
    """
    if not plays:
        return 0

    if _use_cosmos_backend():
        container = _spotify_plays_container()
        normalized = _normalize_lookup_value(profile_id)
        # Count existing plays so we can report how many are *new* this upload.
        # Re-uploads still upsert every doc, which refreshes their 90-day TTL.
        before_items = list(container.query_items(
            query="SELECT VALUE COUNT(1) FROM c WHERE c.profile_id_normalized = @p",
            parameters=[{"name": "@p", "value": normalized}],
            partition_key=normalized,
        ))
        before = int(before_items[0]) if before_items else 0

        docs = [_spotify_play_doc(profile_id, p) for p in plays]

        def _upsert_one(doc):
            try:
                container.upsert_item(body=doc)
            except cosmos_exceptions.CosmosHttpResponseError:
                # SDK already retries 429s; surface other errors as a skip.
                return False
            return True

        with ThreadPoolExecutor(max_workers=max(1, SPOTIFY_BULK_INSERT_WORKERS)) as ex:
            list(ex.map(_upsert_one, docs))

        after_items = list(container.query_items(
            query="SELECT VALUE COUNT(1) FROM c WHERE c.profile_id_normalized = @p",
            parameters=[{"name": "@p", "value": normalized}],
            partition_key=normalized,
        ))
        after = int(after_items[0]) if after_items else before
        return max(0, after - before)

    _sqlite_init_db()
    rows = [
        (
            profile_id,
            p["track"],
            p["artist"],
            p.get("album", "") or "",
            p["played_at"],
            int(p["played_at_unix"]),
            int(p.get("ms_played", 0) or 0),
        )
        for p in plays
    ]
    with _sqlite_connect() as conn:
        before = conn.execute(
            "SELECT COUNT(*) AS c FROM spotify_history WHERE LOWER(profile_id) = LOWER(?)",
            (profile_id,),
        ).fetchone()["c"]
        conn.executemany(
            """
            INSERT OR IGNORE INTO spotify_history
                (profile_id, track, artist, album, played_at, played_at_unix, ms_played)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        conn.commit()
        after = conn.execute(
            "SELECT COUNT(*) AS c FROM spotify_history WHERE LOWER(profile_id) = LOWER(?)",
            (profile_id,),
        ).fetchone()["c"]
    return int(after) - int(before)


def has_spotify_data(profile_id: str) -> bool:
    if _use_cosmos_backend():
        container = _spotify_plays_container()
        normalized = _normalize_lookup_value(profile_id)
        items = list(container.query_items(
            query="SELECT TOP 1 c.id FROM c WHERE c.profile_id_normalized = @p",
            parameters=[{"name": "@p", "value": normalized}],
            partition_key=normalized,
        ))
        return bool(items)

    _sqlite_init_db()
    with _sqlite_connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM spotify_history WHERE LOWER(profile_id) = LOWER(?) LIMIT 1",
            (profile_id,),
        ).fetchone()
    return row is not None


def get_spotify_first_listen(profile_id: str, track: str, artist: str) -> dict | None:
    """Return the earliest play of *(track, artist)* for the profile, or None."""
    if _use_cosmos_backend():
        container = _spotify_plays_container()
        normalized = _normalize_lookup_value(profile_id)
        items = list(container.query_items(
            query=(
                "SELECT TOP 1 c.track, c.artist, c.album, c.played_at, c.played_at_unix "
                "FROM c WHERE c.profile_id_normalized = @p "
                "AND c.track_normalized = @t AND c.artist_normalized = @a "
                "ORDER BY c.played_at_unix ASC"
            ),
            parameters=[
                {"name": "@p", "value": normalized},
                {"name": "@t", "value": _normalize_lookup_value(track)},
                {"name": "@a", "value": _normalize_lookup_value(artist)},
            ],
            partition_key=normalized,
        ))
        return items[0] if items else None

    _sqlite_init_db()
    with _sqlite_connect() as conn:
        row = conn.execute(
            """
            SELECT track, artist, album, played_at, played_at_unix
            FROM spotify_history
            WHERE LOWER(profile_id) = LOWER(?)
              AND LOWER(track)      = LOWER(?)
              AND LOWER(artist)     = LOWER(?)
            ORDER BY played_at_unix ASC
            LIMIT 1
            """,
            (profile_id, track, artist),
        ).fetchone()
    return dict(row) if row else None


def get_spotify_artist_first_listen(profile_id: str, artist: str) -> dict | None:
    """Return the earliest play of any track by *artist* for the profile."""
    if _use_cosmos_backend():
        container = _spotify_plays_container()
        normalized = _normalize_lookup_value(profile_id)
        items = list(container.query_items(
            query=(
                "SELECT TOP 1 c.track, c.artist, c.album, c.played_at, c.played_at_unix "
                "FROM c WHERE c.profile_id_normalized = @p AND c.artist_normalized = @a "
                "ORDER BY c.played_at_unix ASC"
            ),
            parameters=[
                {"name": "@p", "value": normalized},
                {"name": "@a", "value": _normalize_lookup_value(artist)},
            ],
            partition_key=normalized,
        ))
        return items[0] if items else None

    _sqlite_init_db()
    with _sqlite_connect() as conn:
        row = conn.execute(
            """
            SELECT track, artist, album, played_at, played_at_unix
            FROM spotify_history
            WHERE LOWER(profile_id) = LOWER(?)
              AND LOWER(artist)     = LOWER(?)
            ORDER BY played_at_unix ASC
            LIMIT 1
            """,
            (profile_id, artist),
        ).fetchone()
    return dict(row) if row else None


def get_spotify_play_count(profile_id: str, track: str, artist: str) -> int:
    if _use_cosmos_backend():
        container = _spotify_plays_container()
        normalized = _normalize_lookup_value(profile_id)
        items = list(container.query_items(
            query=(
                "SELECT VALUE COUNT(1) FROM c WHERE c.profile_id_normalized = @p "
                "AND c.track_normalized = @t AND c.artist_normalized = @a"
            ),
            parameters=[
                {"name": "@p", "value": normalized},
                {"name": "@t", "value": _normalize_lookup_value(track)},
                {"name": "@a", "value": _normalize_lookup_value(artist)},
            ],
            partition_key=normalized,
        ))
        return int(items[0]) if items else 0

    _sqlite_init_db()
    with _sqlite_connect() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM spotify_history
            WHERE LOWER(profile_id) = LOWER(?)
              AND LOWER(track)      = LOWER(?)
              AND LOWER(artist)     = LOWER(?)
            """,
            (profile_id, track, artist),
        ).fetchone()
    return int(row["c"] or 0)


def clear_spotify_data(profile_id: str) -> int:
    """Delete all imported plays for *profile_id*. Returns rows deleted."""
    if _use_cosmos_backend():
        container = _spotify_plays_container()
        normalized = _normalize_lookup_value(profile_id)
        # Count first; the partition-key delete returns no count.
        count_items = list(container.query_items(
            query="SELECT VALUE COUNT(1) FROM c WHERE c.profile_id_normalized = @p",
            parameters=[{"name": "@p", "value": normalized}],
            partition_key=normalized,
        ))
        count = int(count_items[0]) if count_items else 0
        if count == 0:
            return 0
        # Try the bulk partition-key delete first (fast, single request). It
        # may not be available on the SDK (AttributeError) or may be disabled
        # at the account level (CosmosHttpResponseError 400/403). Fall back to
        # per-item deletes in either case so the operation always succeeds.
        bulk_ok = False
        try:
            container.delete_all_items_by_partition_key(normalized)
            bulk_ok = True
        except AttributeError:
            pass
        except cosmos_exceptions.CosmosHttpResponseError:
            pass
        if not bulk_ok:
            ids = list(container.query_items(
                query="SELECT c.id FROM c WHERE c.profile_id_normalized = @p",
                parameters=[{"name": "@p", "value": normalized}],
                partition_key=normalized,
            ))
            for item in ids:
                try:
                    container.delete_item(item=item["id"], partition_key=normalized)
                except cosmos_exceptions.CosmosResourceNotFoundError:
                    pass
        return count

    _sqlite_init_db()
    with _sqlite_connect() as conn:
        cur = conn.execute(
            "DELETE FROM spotify_history WHERE LOWER(profile_id) = LOWER(?)",
            (profile_id,),
        )
        conn.commit()
        return cur.rowcount or 0


def delete_spotify_profile(profile_id: str) -> None:
    delete_all_spotify_sessions(profile_id)
    if _use_cosmos_backend():
        clear_spotify_data(profile_id)
        profiles = _spotify_profiles_container()
        normalized = _normalize_lookup_value(profile_id)
        try:
            profiles.delete_item(item=normalized, partition_key=normalized)
        except cosmos_exceptions.CosmosResourceNotFoundError:
            pass
        return

    _sqlite_init_db()
    with _sqlite_connect() as conn:
        conn.execute(
            "DELETE FROM spotify_history WHERE LOWER(profile_id) = LOWER(?)",
            (profile_id,),
        )
        conn.execute(
            "DELETE FROM spotify_profiles WHERE LOWER(profile_id) = LOWER(?)",
            (profile_id,),
        )
        conn.commit()


def search_spotify_tracks(profile_id: str, query: str, limit: int = 20) -> list[dict]:
    """LIKE-based autocomplete search over imported Spotify tracks."""
    if not query or not query.strip():
        return []
    if _use_cosmos_backend():
        container = _spotify_plays_container()
        normalized = _normalize_lookup_value(profile_id)
        needle = _normalize_lookup_value(query)
        # GROUP BY in Cosmos doesn't support ORDER BY on aggregates without
        # composite indexes; do the ranking client-side over a bounded result set.
        items = list(container.query_items(
            query=(
                "SELECT c.track, c.artist, c.album, c.track_normalized, "
                "c.artist_normalized, c.played_at_unix "
                "FROM c WHERE c.profile_id_normalized = @p "
                "AND (CONTAINS(c.track_normalized, @q) OR CONTAINS(c.artist_normalized, @q)) "
                "ORDER BY c.played_at_unix DESC OFFSET 0 LIMIT 5000"
            ),
            parameters=[
                {"name": "@p", "value": normalized},
                {"name": "@q", "value": needle},
            ],
            partition_key=normalized,
        ))
        agg: dict[tuple[str, str], dict] = {}
        for it in items:
            key = (it.get("track_normalized", ""), it.get("artist_normalized", ""))
            existing = agg.get(key)
            played = int(it.get("played_at_unix") or 0)
            if existing is None:
                agg[key] = {
                    "track": it.get("track", ""),
                    "artist": it.get("artist", ""),
                    "album": it.get("album", ""),
                    "first_played": played,
                    "_count": 1,
                }
            else:
                existing["_count"] += 1
                if played < existing["first_played"]:
                    existing["first_played"] = played
        ranked = sorted(agg.values(), key=lambda d: d["_count"], reverse=True)[: int(limit)]
        for r in ranked:
            r.pop("_count", None)
        return ranked

    _sqlite_init_db()
    pattern = f"%{_normalize_lookup_value(query)}%"
    with _sqlite_connect() as conn:
        rows = conn.execute(
            """
            SELECT track, artist, album, MIN(played_at_unix) AS first_played
            FROM spotify_history
            WHERE LOWER(profile_id) = LOWER(?)
              AND (LOWER(track) LIKE ? OR LOWER(artist) LIKE ?)
            GROUP BY LOWER(track), LOWER(artist)
            ORDER BY COUNT(*) DESC
            LIMIT ?
            """,
            (profile_id, pattern, pattern, int(limit)),
        ).fetchall()
    return [dict(r) for r in rows]


SPOTIFY_HISTORY_USERNAME_PREFIX = "spotify:"


def spotify_history_username(profile_id: str) -> str:
    """Namespaced username key used to store Spotify-resolved first-listens.

    The ``searches`` cache table is keyed by ``username``; using a prefix here
    lets a Spotify profile share that cache without colliding with any
    Last.fm username. The prefix is opaque to callers — they pass the bare
    profile id and we round-trip it through this helper.
    """
    return f"{SPOTIFY_HISTORY_USERNAME_PREFIX}{profile_id}"


# ---- Aggregate queries over imported Spotify plays --------------------------

# Period -> seconds back from now. Mirrors Last.fm's user.getTopTracks
# `period` parameter so the front-end can use the same control for both
# sources.
_SPOTIFY_PERIOD_SECONDS: dict[str, int | None] = {
    "7day": 7 * 24 * 60 * 60,
    "1month": 30 * 24 * 60 * 60,
    "3month": 90 * 24 * 60 * 60,
    "6month": 180 * 24 * 60 * 60,
    "12month": 365 * 24 * 60 * 60,
    "overall": None,
}


def _spotify_period_cutoff_unix(period: str) -> int:
    """Return the unix timestamp cutoff for *period*, or 0 for 'overall'.

    Unknown period values fall back to the same default as Last.fm
    (``1month``) rather than raising — this matches the behaviour of the
    existing Last.fm endpoints which silently accept any period and let the
    upstream API decide.
    """
    seconds = _SPOTIFY_PERIOD_SECONDS.get(period, _SPOTIFY_PERIOD_SECONDS["1month"])
    if seconds is None:
        return 0
    return int(datetime.now(timezone.utc).timestamp()) - int(seconds)


def get_spotify_top_tracks(profile_id: str, period: str = "1month", limit: int = 10) -> list[dict]:
    """Return the user's most-played tracks within *period*, newest first.

    Each row contains ``track``, ``artist``, ``album`` and ``playcount``.
    """
    cutoff = _spotify_period_cutoff_unix(period)
    if _use_cosmos_backend():
        container = _spotify_plays_container()
        normalized = _normalize_lookup_value(profile_id)
        if cutoff > 0:
            query = (
                "SELECT c.track, c.artist, c.album, c.track_normalized, "
                "c.artist_normalized FROM c "
                "WHERE c.profile_id_normalized = @p "
                "AND c.played_at_unix >= @cutoff"
            )
            params = [
                {"name": "@p", "value": normalized},
                {"name": "@cutoff", "value": int(cutoff)},
            ]
        else:
            query = (
                "SELECT c.track, c.artist, c.album, c.track_normalized, "
                "c.artist_normalized FROM c "
                "WHERE c.profile_id_normalized = @p"
            )
            params = [{"name": "@p", "value": normalized}]
        items = list(container.query_items(
            query=query, parameters=params, partition_key=normalized,
        ))
        agg: dict[tuple[str, str], dict] = {}
        for it in items:
            key = (it.get("track_normalized", ""), it.get("artist_normalized", ""))
            existing = agg.get(key)
            if existing is None:
                agg[key] = {
                    "track": it.get("track", ""),
                    "artist": it.get("artist", ""),
                    "album": it.get("album", ""),
                    "playcount": 1,
                }
            else:
                existing["playcount"] += 1
        ranked = sorted(agg.values(), key=lambda d: d["playcount"], reverse=True)
        return ranked[: int(limit)]

    _sqlite_init_db()
    with _sqlite_connect() as conn:
        if cutoff > 0:
            rows = conn.execute(
                """
                SELECT track, artist, MAX(album) AS album, COUNT(*) AS playcount
                FROM spotify_history
                WHERE LOWER(profile_id) = LOWER(?)
                  AND played_at_unix >= ?
                GROUP BY LOWER(track), LOWER(artist)
                ORDER BY playcount DESC, MAX(played_at_unix) DESC
                LIMIT ?
                """,
                (profile_id, int(cutoff), int(limit)),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT track, artist, MAX(album) AS album, COUNT(*) AS playcount
                FROM spotify_history
                WHERE LOWER(profile_id) = LOWER(?)
                GROUP BY LOWER(track), LOWER(artist)
                ORDER BY playcount DESC, MAX(played_at_unix) DESC
                LIMIT ?
                """,
                (profile_id, int(limit)),
            ).fetchall()
    return [dict(r) for r in rows]


def get_spotify_recent_tracks(profile_id: str, limit: int = 10) -> list[dict]:
    """Return the most recently played tracks for *profile_id*."""
    if _use_cosmos_backend():
        container = _spotify_plays_container()
        normalized = _normalize_lookup_value(profile_id)
        items = list(container.query_items(
            query=(
                "SELECT c.track, c.artist, c.album, c.played_at, c.played_at_unix "
                "FROM c WHERE c.profile_id_normalized = @p "
                "ORDER BY c.played_at_unix DESC OFFSET 0 LIMIT @lim"
            ),
            parameters=[
                {"name": "@p", "value": normalized},
                {"name": "@lim", "value": int(limit)},
            ],
            partition_key=normalized,
        ))
        return [
            {
                "track": it.get("track", ""),
                "artist": it.get("artist", ""),
                "album": it.get("album", ""),
                "played_at": it.get("played_at", ""),
                "played_at_unix": int(it.get("played_at_unix") or 0),
            }
            for it in items
        ]

    _sqlite_init_db()
    with _sqlite_connect() as conn:
        rows = conn.execute(
            """
            SELECT track, artist, album, played_at, played_at_unix
            FROM spotify_history
            WHERE LOWER(profile_id) = LOWER(?)
            ORDER BY played_at_unix DESC
            LIMIT ?
            """,
            (profile_id, int(limit)),
        ).fetchall()
    return [dict(r) for r in rows]


def get_spotify_plays_in_range(profile_id: str, from_unix: int, to_unix: int) -> list[dict]:
    """Return all plays for *profile_id* between *from_unix* and *to_unix* (inclusive lower, exclusive upper)."""
    if _use_cosmos_backend():
        container = _spotify_plays_container()
        normalized = _normalize_lookup_value(profile_id)
        items = list(container.query_items(
            query=(
                "SELECT c.track, c.artist, c.album, c.played_at, c.played_at_unix "
                "FROM c WHERE c.profile_id_normalized = @p "
                "AND c.played_at_unix >= @f AND c.played_at_unix < @t "
                "ORDER BY c.played_at_unix ASC"
            ),
            parameters=[
                {"name": "@p", "value": normalized},
                {"name": "@f", "value": int(from_unix)},
                {"name": "@t", "value": int(to_unix)},
            ],
            partition_key=normalized,
        ))
        return [
            {
                "track": it.get("track", ""),
                "artist": it.get("artist", ""),
                "album": it.get("album", ""),
                "played_at": it.get("played_at", ""),
                "played_at_unix": int(it.get("played_at_unix") or 0),
            }
            for it in items
        ]

    _sqlite_init_db()
    with _sqlite_connect() as conn:
        rows = conn.execute(
            """
            SELECT track, artist, album, played_at, played_at_unix
            FROM spotify_history
            WHERE LOWER(profile_id) = LOWER(?)
              AND played_at_unix >= ? AND played_at_unix < ?
            ORDER BY played_at_unix ASC
            """,
            (profile_id, int(from_unix), int(to_unix)),
        ).fetchall()
    return [dict(r) for r in rows]


def get_spotify_track_play_timestamps(
    profile_id: str, track: str, artist: str
) -> list[int]:
    """Return every play timestamp (unix seconds) for *(track, artist)*, ascending."""
    if _use_cosmos_backend():
        container = _spotify_plays_container()
        normalized = _normalize_lookup_value(profile_id)
        items = list(container.query_items(
            query=(
                "SELECT c.played_at_unix FROM c "
                "WHERE c.profile_id_normalized = @p "
                "AND c.track_normalized = @t AND c.artist_normalized = @a "
                "ORDER BY c.played_at_unix ASC"
            ),
            parameters=[
                {"name": "@p", "value": normalized},
                {"name": "@t", "value": _normalize_lookup_value(track)},
                {"name": "@a", "value": _normalize_lookup_value(artist)},
            ],
            partition_key=normalized,
        ))
        return [int(it.get("played_at_unix") or 0) for it in items]

    _sqlite_init_db()
    with _sqlite_connect() as conn:
        rows = conn.execute(
            """
            SELECT played_at_unix
            FROM spotify_history
            WHERE LOWER(profile_id) = LOWER(?)
              AND LOWER(track)      = LOWER(?)
              AND LOWER(artist)     = LOWER(?)
            ORDER BY played_at_unix ASC
            """,
            (profile_id, track, artist),
        ).fetchall()
    return [int(r["played_at_unix"]) for r in rows]


def get_spotify_stats(profile_id: str) -> dict:
    if _use_cosmos_backend():
        container = _spotify_plays_container()
        normalized = _normalize_lookup_value(profile_id)
        agg = list(container.query_items(
            query=(
                "SELECT VALUE { total: COUNT(1), earliest: MIN(c.played_at), "
                "latest: MAX(c.played_at) } FROM c WHERE c.profile_id_normalized = @p"
            ),
            parameters=[{"name": "@p", "value": normalized}],
            partition_key=normalized,
        ))
        totals = agg[0] if agg else {}
        # Distinct counts via GROUP BY (one row per distinct value).
        track_groups = list(container.query_items(
            query=(
                "SELECT c.track_normalized, c.artist_normalized FROM c "
                "WHERE c.profile_id_normalized = @p "
                "GROUP BY c.track_normalized, c.artist_normalized"
            ),
            parameters=[{"name": "@p", "value": normalized}],
            partition_key=normalized,
        ))
        artist_groups = list(container.query_items(
            query=(
                "SELECT c.artist_normalized FROM c "
                "WHERE c.profile_id_normalized = @p "
                "GROUP BY c.artist_normalized"
            ),
            parameters=[{"name": "@p", "value": normalized}],
            partition_key=normalized,
        ))
        return {
            "total_plays": int(totals.get("total") or 0),
            "unique_tracks": len(track_groups),
            "unique_artists": len(artist_groups),
            "earliest": totals.get("earliest") or "",
            "latest": totals.get("latest") or "",
        }

    _sqlite_init_db()
    with _sqlite_connect() as conn:
        row = conn.execute(
            """
            SELECT
                COUNT(*)                                           AS total_plays,
                COUNT(DISTINCT LOWER(track) || '|' || LOWER(artist)) AS unique_tracks,
                COUNT(DISTINCT LOWER(artist))                      AS unique_artists,
                MIN(played_at)                                     AS earliest,
                MAX(played_at)                                     AS latest
            FROM spotify_history
            WHERE LOWER(profile_id) = LOWER(?)
            """,
            (profile_id,),
        ).fetchone()
    return {
        "total_plays": int(row["total_plays"] or 0),
        "unique_tracks": int(row["unique_tracks"] or 0),
        "unique_artists": int(row["unique_artists"] or 0),
        "earliest": row["earliest"] or "",
        "latest": row["latest"] or "",
    }


