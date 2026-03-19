"""
IPTV API — JSON endpoints, auth via secret_token.

Endpoints:
  GET  /api/v1/status                         — health check
  POST /api/v1/auth                           — validate token → user + packages info
  GET  /api/v1/playlist/<token>.m3u           — M3U playlist with personal stream URLs
  GET  /api/v1/epg/<token>                    — EPG redirect / stub
  POST /api/v1/stream/<token>/<sid>/start     — register stream session (concurrent limit check)
  POST /api/v1/stream/<token>/<sid>/stop      — release stream session
  POST /api/v1/stream/<token>/ping            — heartbeat (keep session alive)
  GET  /api/v1/token/validate                 — quick token check
"""
from flask import Blueprint, request, jsonify, Response
from datetime import datetime, timedelta
from .db import get_db
from .auth import rate_limit

bp = Blueprint('api', __name__, url_prefix='/api/v1')

STREAM_TTL_SECONDS = 30   # session expires if no ping for 30s

def api_err(msg: str, code: int = 400):
    return jsonify({'ok': False, 'error': msg}), code

def get_ip():
    return (request.headers.get('X-Forwarded-For') or request.remote_addr or '').split(',')[0].strip()

def _get_user(db, token: str):
    """Fetch user by secret_token, check not blocked/unconfirmed."""
    if not token or len(token) > 32:
        return None
    return db.execute(
        "SELECT * FROM users WHERE secret_token=? AND is_blocked=0 AND email_confirmed=1",
        (token,)).fetchone()

def _user_packages(db, user_id: int):
    """Return list of active packages for user."""
    return db.execute("""
        SELECT pk.id, pk.name, pk.connections, pk.price_per_day,
               s.name as service_name, s.icon as service_icon
        FROM user_packages up
        JOIN packages pk ON up.package_id = pk.id
        JOIN services  s  ON pk.service_id = s.id
        WHERE up.user_id = ? AND up.active = 1
        ORDER BY s.sort_order, pk.sort_order""", (user_id,)).fetchall()

def _max_connections(db, user_id: int) -> int:
    """Total simultaneous connections allowed."""
    row = db.execute("""
        SELECT COALESCE(SUM(pk.connections), 0) as total
        FROM user_packages up JOIN packages pk ON up.package_id = pk.id
        WHERE up.user_id = ? AND up.active = 1""", (user_id,)).fetchone()
    return int(row['total']) if row else 0

def _active_streams(db, user_id: int) -> int:
    """Count currently active (non-expired) streams."""
    db.execute("""DELETE FROM active_streams
        WHERE user_id = ? AND last_ping < datetime('now', ?)""",
        (user_id, f'-{STREAM_TTL_SECONDS} seconds'))
    row = db.execute(
        "SELECT COUNT(*) FROM active_streams WHERE user_id=?", (user_id,)).fetchone()
    return row[0]

def _has_active_packages(db, user_id: int) -> bool:
    row = db.execute(
        "SELECT 1 FROM user_packages WHERE user_id=? AND active=1 LIMIT 1",
        (user_id,)).fetchone()
    return bool(row)

def _get_site_url(db) -> str:
    row = db.execute("SELECT site_url FROM smtp_settings WHERE id=1").fetchone()
    return (row['site_url'] if row else 'http://localhost:5003').rstrip('/')

# ── HEALTH ────────────────────────────────────────────────────────────────────
@bp.route('/status', methods=['GET'])
def status():
    db = get_db()
    try:
        db.execute("SELECT 1").fetchone()
        db_ok = True
    except Exception:
        db_ok = False
    return jsonify({'ok': True, 'db': db_ok, 'ts': datetime.utcnow().isoformat()})

# ── AUTH ──────────────────────────────────────────────────────────────────────
@bp.route('/auth', methods=['POST'])
def auth():
    """Validate token, return user info and active packages."""
    ip = get_ip()
    if not rate_limit(f'api_auth:{ip}', 30, 60):
        return api_err('Rate limit exceeded', 429)
    data  = request.get_json(silent=True) or request.form
    token = (data.get('token') or '').strip()
    if not token:
        return api_err('token required', 400)
    db   = get_db()
    user = _get_user(db, token)
    if not user:
        rate_limit(f'api_bad:{ip}', 10, 300)
        return api_err('invalid token', 401)
    pkgs     = _user_packages(db, user['id'])
    max_conn = _max_connections(db, user['id'])
    active   = _active_streams(db, user['id'])
    site_url = _get_site_url(db)
    return jsonify({
        'ok': True,
        'user': {
            'id':            user['id'],
            'login':         user['login'],
            'balance':       round(user['balance'], 2),
            'stream_format': user['stream_format'] or 'ts',
        },
        'subscription': {
            'active':       len(pkgs) > 0,
            'packages':     [{'name': p['name'], 'service': p['service_name'],
                               'connections': p['connections']} for p in pkgs],
            'max_connections': max_conn,
            'active_streams':  active,
        },
        'playlist_url': f'{site_url}/api/v1/playlist/{token}.m3u',
    })

# ── PLAYLIST ──────────────────────────────────────────────────────────────────
@bp.route('/playlist/<token>.m3u', methods=['GET'])
def playlist(token):
    """Return M3U playlist. Stream URLs contain user token for concurrency tracking."""
    ip = get_ip()
    if not rate_limit(f'api_plist:{ip}', 20, 60):
        return api_err('Rate limit exceeded', 429)
    db   = get_db()
    user = _get_user(db, token)
    if not user:
        return api_err('invalid token', 401)
    if not _has_active_packages(db, user['id']):
        return api_err('no active subscription', 403)
    site_url = _get_site_url(db)
    fmt      = user['stream_format'] or 'ts'
    server   = user['preferred_server'] or 'auto'
    # Build M3U — stub channels for now; real proxy replaces stream URLs
    pkgs = _user_packages(db, user['id'])
    lines = ['#EXTM3U x-tvg-url="" url-tvg=""']
    for pkg in pkgs:
        lines.append(
            f'#EXTINF:-1 group-title="{pkg["service_name"]}" tvg-id="" tvg-name="{pkg["name"]}",'
            f'{pkg["service_icon"]} {pkg["name"]}'
        )
        # Stream URL contains token and stream_id for concurrency control
        # Real proxy will expand this into actual channel list
        lines.append(
            f'{site_url}/api/v1/stream/{token}/pkg{pkg["id"]}/start?fmt={fmt}&srv={server}'
        )
    m3u = '\n'.join(lines)
    return Response(m3u, mimetype='audio/x-mpegurl',
                    headers={'Content-Disposition': f'inline; filename="playlist_{token}.m3u"'})

# ── EPG ───────────────────────────────────────────────────────────────────────
@bp.route('/epg/<token>', methods=['GET'])
def epg(token):
    """EPG endpoint — stub, returns empty XMLTV."""
    db   = get_db()
    user = _get_user(db, token)
    if not user:
        return api_err('invalid token', 401)
    xml = ('<?xml version="1.0" encoding="utf-8"?>'
           '<!DOCTYPE tv SYSTEM "xmltv.dtd">'
           '<tv generator-info-name="IPTV Billing"></tv>')
    return Response(xml, mimetype='application/xml')

# ── STREAM SESSION ────────────────────────────────────────────────────────────
@bp.route('/stream/<token>/<stream_id>/start', methods=['GET', 'POST'])
def stream_start(token, stream_id):
    """
    Called when a client starts a stream.
    Checks concurrent connection limit and registers the session.
    """
    ip = get_ip()
    if not rate_limit(f'api_stream:{ip}', 60, 60):
        return api_err('Rate limit exceeded', 429)
    db   = get_db()
    user = _get_user(db, token)
    if not user:
        return api_err('invalid token', 401)
    if not _has_active_packages(db, user['id']):
        return api_err('subscription inactive', 403)
    max_conn = _max_connections(db, user['id'])
    # Expire stale sessions first
    with db:
        db.execute("""DELETE FROM active_streams
            WHERE user_id=? AND last_ping < datetime('now', ?)""",
            (user['id'], f'-{STREAM_TTL_SECONDS} seconds'))
    active = db.execute(
        "SELECT COUNT(*) FROM active_streams WHERE user_id=?",
        (user['id'],)).fetchone()[0]
    # Check if this stream_id already registered (resume)
    existing = db.execute(
        "SELECT id FROM active_streams WHERE user_id=? AND stream_id=?",
        (user['id'], stream_id)).fetchone()
    if existing:
        with db:
            db.execute("UPDATE active_streams SET last_ping=datetime('now'), client_ip=? WHERE id=?",
                       (ip, existing['id']))
        return jsonify({'ok': True, 'stream_id': stream_id,
                        'active': active, 'max': max_conn})
    if active >= max_conn:
        return jsonify({
            'ok': False,
            'error': 'concurrent_limit_reached',
            'active': active,
            'max': max_conn,
            'message': f'Достигнут лимит {max_conn} одновременных подключений.'
        }), 403
    with db:
        db.execute("""INSERT OR REPLACE INTO active_streams
            (user_id, stream_id, client_ip) VALUES (?,?,?)""",
            (user['id'], stream_id, ip))
    return jsonify({'ok': True, 'stream_id': stream_id,
                    'active': active + 1, 'max': max_conn})

@bp.route('/stream/<token>/<stream_id>/stop', methods=['POST'])
def stream_stop(token, stream_id):
    """Release a stream session."""
    db   = get_db()
    user = _get_user(db, token)
    if not user:
        return api_err('invalid token', 401)
    with db:
        db.execute("DELETE FROM active_streams WHERE user_id=? AND stream_id=?",
                   (user['id'], stream_id))
    return jsonify({'ok': True})

@bp.route('/stream/<token>/ping', methods=['POST'])
def stream_ping(token):
    """
    Heartbeat — keeps all sessions alive.
    Client should call every ~15s. Sessions expire after STREAM_TTL_SECONDS.
    Body (optional): { "stream_ids": ["sid1", "sid2"] }
    """
    ip = get_ip()
    if not rate_limit(f'api_ping:{ip}', 120, 60):
        return api_err('Rate limit exceeded', 429)
    db   = get_db()
    user = _get_user(db, token)
    if not user:
        return api_err('invalid token', 401)
    data       = request.get_json(silent=True) or {}
    stream_ids = data.get('stream_ids') or []
    with db:
        if stream_ids:
            for sid in stream_ids[:16]:  # max 16 per ping
                db.execute("""UPDATE active_streams SET last_ping=datetime('now')
                    WHERE user_id=? AND stream_id=?""", (user['id'], str(sid)[:64]))
        else:
            db.execute("UPDATE active_streams SET last_ping=datetime('now') WHERE user_id=?",
                       (user['id'],))
    active = _active_streams(db, user['id'])
    return jsonify({'ok': True, 'active_streams': active,
                    'max_connections': _max_connections(db, user['id']),
                    'ttl_seconds': STREAM_TTL_SECONDS})

# ── TOKEN VALIDATE ────────────────────────────────────────────────────────────
@bp.route('/token/validate', methods=['GET'])
def token_validate():
    """Quick token check. GET ?token=XXXXXX"""
    ip    = get_ip()
    token = request.args.get('token', '').strip()
    if not rate_limit(f'api_val:{ip}', 20, 60):
        return api_err('Rate limit exceeded', 429)
    if not token:
        return api_err('token required', 400)
    db   = get_db()
    user = db.execute(
        "SELECT id, is_blocked, email_confirmed FROM users WHERE secret_token=?",
        (token,)).fetchone()
    if not user:
        return api_err('not found', 404)
    has_sub = _has_active_packages(db, user['id'])
    return jsonify({
        'ok':    True,
        'valid': not user['is_blocked'] and bool(user['email_confirmed']) and has_sub,
        'subscription_active': has_sub,
    })
