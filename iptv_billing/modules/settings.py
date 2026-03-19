"""Site settings accessor — reads from site_settings table with in-memory cache."""
from .db import get_db

_cache = {}
_cache_ts = 0
CACHE_TTL = 60  # seconds

def _load():
    global _cache, _cache_ts
    import time
    now = time.monotonic()
    if _cache and now - _cache_ts < CACHE_TTL:
        return _cache
    try:
        db = get_db()
        rows = db.execute("SELECT key, value FROM site_settings").fetchall()
        _cache = {r['key']: r['value'] for r in rows}
        _cache_ts = now
    except Exception:
        pass
    return _cache

def get(key: str, default=None):
    return _load().get(key, default)

def get_bool(key: str, default=True) -> bool:
    v = _load().get(key)
    if v is None: return default
    return v.strip() not in ('0', 'false', 'no', '')

def get_int(key: str, default=0) -> int:
    try: return int(_load().get(key, default))
    except (ValueError, TypeError): return default

def get_float(key: str, default=0.0) -> float:
    try: return float(_load().get(key, default))
    except (ValueError, TypeError): return default

def get_list(key: str, default=None):
    v = _load().get(key, '')
    items = [x.strip() for x in v.split(',') if x.strip()]
    return items if items else (default or [])

def invalidate():
    global _cache_ts
    _cache_ts = 0

def all_by_group():
    db   = get_db()
    rows = db.execute("SELECT * FROM site_settings ORDER BY group_, key").fetchall()
    result = {}
    for r in rows:
        g = r['group_']
        result.setdefault(g, []).append(dict(r))
    return result

def save(key: str, value: str):
    db = get_db()
    with db:
        db.execute("""INSERT INTO site_settings (key, value, updated_at)
            VALUES (?, ?, datetime('now'))
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
            (key, value))
    invalidate()
