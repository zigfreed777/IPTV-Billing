"""
Proxy Agent Client
Sends POST /config to the IPTV proxy agent (app.py) running on each server.

Agent API (app.py):
  POST http://<server_ip>:<api_port>/config
  Body: JSON patch — see local.json for full config schema.

Operations used:
  clients.add    — register new user on a server
  clients.update — update user params (token, stream count, adult flag, expiry)
  clients.delete — remove user from a server

expiredAt logic
───────────────
The proxy has no concept of "balance". Instead it uses a Unix timestamp (int)
that marks when the subscription ends. We calculate it from the user's current
balance and the combined effective daily cost of all active packages, factoring in:
  - active percent/fixed promo codes applied to each package  (reduce daily rate)
  - days-type promo codes                                     (add free days)
  - extra connection surcharges  (extra_connections * extra_connection_price/day)

NOTE on global promos (package_id IS NULL):
  A global promo is applied ONCE to the total daily cost, not multiplied per
  package. We apply it as a flat fixed discount to the grand total.
"""
import logging, requests
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

AGENT_TIMEOUT = 8   # seconds per request


# ── Low-level HTTP ────────────────────────────────────────────────────────────

def _agent_url(server) -> str:
    return f'http://{server["ip"]}:{server["api_port"]}'

def _headers(server) -> dict:
    h = {'Content-Type': 'application/json'}
    token = server['api_token'] if 'api_token' in server.keys() else ''
    if token:
        h['X-Auth-Token'] = token
    return h

def _post(server, payload: dict) -> tuple[bool, str]:
    """POST /config to agent. Returns (ok, message)."""
    url = _agent_url(server) + '/config'
    try:
        r = requests.post(url, json=payload,
                          headers=_headers(server), timeout=AGENT_TIMEOUT)
        if r.status_code == 200:
            return True, f'ok reload={r.json().get("reload")}'
        return False, f'HTTP {r.status_code}: {r.text[:200]}'
    except requests.exceptions.ConnectionError:
        return False, f'Connection refused {server["ip"]}:{server["api_port"]}'
    except requests.exceptions.Timeout:
        return False, f'Timeout {server["ip"]}:{server["api_port"]}'
    except Exception as e:
        return False, str(e)


# ── expiredAt calculation ─────────────────────────────────────────────────────

def _effective_daily_cost(db, user_id: int) -> float:
    """
    Total effective daily cost for a user after all discounts.

    Per-package discounts (percent / fixed promos bound to a specific package_id):
      applied individually to each package's price_per_day.

    Global discounts (promos with package_id IS NULL):
      applied ONCE as a fixed-₽ reduction of the grand total, NOT per-package.

    Extra connections add:
      extra_connections * extra_connection_price  per package.

    Returns total ₽/day (float ≥ 0).
    """
    rows = db.execute("""
        SELECT
            up.id                   AS up_id,
            up.extra_connections,
            pk.id                   AS pkg_id,
            pk.price_per_day,
            pk.extra_connection_price
        FROM user_packages up
        JOIN packages pk ON up.package_id = pk.id
        WHERE up.user_id = ? AND up.active = 1
    """, (user_id,)).fetchall()

    if not rows:
        return 0.0

    subtotal = 0.0
    for row in rows:
        base  = float(row['price_per_day'])
        extra = float(row['extra_connections'] or 0) * float(row['extra_connection_price'] or 0)

        # Per-package promo (latest active percent/fixed promo for this package)
        promo = db.execute("""
            SELECT pc.type, pc.value
            FROM promocode_uses pu
            JOIN promocodes pc ON pu.promocode_id = pc.id
            WHERE pu.user_id = ?
              AND pc.package_id = ?
              AND pc.type IN ('percent', 'fixed')
              AND pc.is_active = 1
            ORDER BY pu.used_at DESC LIMIT 1
        """, (user_id, row['pkg_id'])).fetchone()

        if promo:
            if promo['type'] == 'percent':
                base = round(base * (1.0 - float(promo['value']) / 100.0), 4)
            elif promo['type'] == 'fixed':
                base = max(0.0, base - float(promo['value']))

        subtotal += max(0.0, base) + extra

    # Global promos (no package_id) — apply ONCE to grand total
    global_promos = db.execute("""
        SELECT pc.type, pc.value
        FROM promocode_uses pu
        JOIN promocodes pc ON pu.promocode_id = pc.id
        WHERE pu.user_id = ?
          AND pc.package_id IS NULL
          AND pc.type IN ('percent', 'fixed')
          AND pc.is_active = 1
        ORDER BY pu.used_at DESC
    """, (user_id,)).fetchall()

    for gp in global_promos:
        if gp['type'] == 'percent':
            subtotal = round(subtotal * (1.0 - float(gp['value']) / 100.0), 4)
        elif gp['type'] == 'fixed':
            subtotal = max(0.0, subtotal - float(gp['value']))

    return max(0.0, subtotal)


def _days_promo_bonus(db, user_id: int) -> float:
    """
    Sum of extra free days from all active 'days'-type promos used by this user.
    Each promo is counted once.
    """
    row = db.execute("""
        SELECT COALESCE(SUM(pc.value), 0) AS bonus
        FROM promocode_uses pu
        JOIN promocodes pc ON pu.promocode_id = pc.id
        WHERE pu.user_id = ?
          AND pc.type = 'days'
          AND pc.is_active = 1
    """, (user_id,)).fetchone()
    return float(row['bonus']) if row else 0.0


def calc_expiry_ts(db, user_id: int) -> int:
    """
    Calculate Unix timestamp when the user's balance will run out.

    Formula:
        days_covered = balance / daily_cost   (if daily_cost > 0, else 0)
        days_covered += bonus_days_from_promos
        expiredAt    = now_utc + days_covered * 86400

    Minimum: 1 hour from now so the proxy never blocks immediately.
    """
    user = db.execute("SELECT balance FROM users WHERE id=?", (user_id,)).fetchone()
    if not user:
        return int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp())

    daily = _effective_daily_cost(db, user_id)
    bonus = _days_promo_bonus(db, user_id)

    days = (float(user['balance']) / daily + bonus) if daily > 0 else bonus
    days = max(days, 0.0)

    min_ts = int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp())
    ts     = int((datetime.now(timezone.utc) + timedelta(days=days)).timestamp())
    return max(ts, min_ts)


# ── Client item builder ───────────────────────────────────────────────────────

def _client_n(db, server_id: int, pkg_id: int, exclude_user_id: int = None) -> int:
    """
    Sequential index for this client on the given server+package.

    We count ALL active subscribers of this package who are assigned to this
    server — either explicitly via user_server_prefs, or implicitly (no pref
    means the user lands on ALL servers for the package, so they count too).
    """
    # Users with explicit preference for this server
    q_pref = """
        SELECT COUNT(DISTINCT usp.user_id)
        FROM user_server_prefs usp
        JOIN user_packages up
            ON up.user_id = usp.user_id AND up.package_id = usp.package_id
        WHERE usp.server_id = ? AND usp.package_id = ? AND up.active = 1
    """
    # Users with NO preference (they go to all servers for the package)
    q_nopref = """
        SELECT COUNT(DISTINCT up.user_id)
        FROM user_packages up
        WHERE up.package_id = ? AND up.active = 1
          AND NOT EXISTS (
              SELECT 1 FROM user_server_prefs usp2
              WHERE usp2.user_id = up.user_id AND usp2.package_id = up.package_id
          )
    """
    params_pref   = [server_id, pkg_id]
    params_nopref = [pkg_id]

    if exclude_user_id is not None:
        q_pref   += " AND usp.user_id != ?"
        q_nopref += " AND up.user_id != ?"
        params_pref.append(exclude_user_id)
        params_nopref.append(exclude_user_id)

    n_pref   = db.execute(q_pref,   params_pref).fetchone()[0]
    n_nopref = db.execute(q_nopref, params_nopref).fetchone()[0]
    return int(n_pref) + int(n_nopref)


def _build_client_item(db, user, user_pkg, pkg, server) -> dict:
    """Build the `item` dict sent inside clients.add / clients.update payloads."""
    extra       = int(user_pkg['extra_connections']) if user_pkg and user_pkg.get('extra_connections') else 0
    max_streams = int(pkg['connections']) + extra
    expiry      = calc_expiry_ts(db, user['id'])
    n           = _client_n(db, server['id'], pkg['id'], exclude_user_id=user['id'])

    return {
        'name':           user['login'],
        'password':       user['secret_token'],
        'isAdultAllowed': not bool(user['block_adult'] if 'block_adult' in user.keys() else 0),
        'isHttp':         (user['stream_format'] or 'ts') == 'm3u8',
        'maxStreamCount': max_streams,
        'expiredAt':      expiry,
        'n':              n,
    }


# ── Single-server sync ────────────────────────────────────────────────────────

def sync_user_to_server(db, server, user, user_pkg, pkg, action='update') -> tuple[bool, str]:
    """
    Sync one user to one server.
    action: 'add' | 'update' | 'delete'

    For 'delete', user must be a full user row (needs user['email']).
    """
    email = user['email']
    if action == 'delete':
        payload = {'clients': {'action': 'delete', 'email': email}}
    elif action in ('add', 'update'):
        item    = _build_client_item(db, user, user_pkg, pkg, server)
        payload = {'clients': {'action': action, 'email': email, 'item': item}}
    else:
        return False, f'Unknown action: {action}'

    ok, msg = _post(server, payload)
    expiry_val = payload.get('clients', {}).get('item', {}).get('expiredAt', '-')
    log.info('[agent] %s user=%s server=%s expiry=%s → %s',
             action, user['login'], server['name'], expiry_val, msg)
    return ok, msg


# ── Server resolution ─────────────────────────────────────────────────────────

def _servers_for_pkg(db, user_id: int, pkg_id: int):
    """
    Return the list of servers this user is assigned to for a package.
    If the user has a preference → only that server.
    Otherwise → all active servers for the package.
    """
    pref = db.execute(
        "SELECT server_id FROM user_server_prefs WHERE user_id=? AND package_id=?",
        (user_id, pkg_id)).fetchone()
    if pref:
        return db.execute(
            "SELECT * FROM servers WHERE id=? AND is_active=1",
            (pref['server_id'],)).fetchall()
    return db.execute(
        "SELECT * FROM servers WHERE package_id=? AND is_active=1 ORDER BY sort_order",
        (pkg_id,)).fetchall()


def _all_servers_for_pkg(db, pkg_id: int):
    """Return ALL active servers for a package regardless of user preference."""
    return db.execute(
        "SELECT * FROM servers WHERE package_id=? AND is_active=1 ORDER BY sort_order",
        (pkg_id,)).fetchall()


def sync_user_to_all_servers(db, user, user_pkg, pkg, action='update') -> list:
    """
    Sync a user to all relevant servers for a package.
    Returns list of (server_name, ok, msg).
    """
    servers = _servers_for_pkg(db, user['id'], pkg['id'])
    results = []
    for srv in servers:
        ok, msg = sync_user_to_server(db, srv, user, user_pkg, pkg, action)
        results.append((srv['name'], ok, msg))
    return results


# ── Event-driven public API ───────────────────────────────────────────────────

def sync_on_package_connect(db, user, pkg_id: int) -> list:
    """
    Triggered when: user connects a package (including trial activation).
    Registers the client on all active servers for that package.
    """
    pkg      = db.execute("SELECT * FROM packages WHERE id=?", (pkg_id,)).fetchone()
    user_pkg = db.execute(
        "SELECT * FROM user_packages WHERE user_id=? AND package_id=? AND active=1",
        (user['id'], pkg_id)).fetchone()
    if not pkg or not user_pkg:
        return []
    return sync_user_to_all_servers(db, user, user_pkg, pkg, 'add')


def sync_on_package_disconnect(db, user, pkg_id: int) -> list:
    """
    Triggered when: user disconnects a package.
    Removes the client from ALL servers that carried this package
    (both preferred and non-preferred), because the package is now gone.

    user must be a full user row — email is required for the delete payload.
    """
    pkg = db.execute("SELECT * FROM packages WHERE id=?", (pkg_id,)).fetchone()
    if not pkg:
        return []
    # Delete from ALL servers for this package (pref is irrelevant — pkg is gone)
    servers = _all_servers_for_pkg(db, pkg_id)
    results = []
    for srv in servers:
        ok, msg = sync_user_to_server(db, srv, user, {}, pkg, 'delete')
        results.append((srv['name'], ok, msg))
    return results


def sync_on_server_change(db, user_id: int, pkg_id: int,
                           old_server_id: int, new_server_id: int) -> list:
    """
    Triggered when: user switches preferred server for a package.

    Steps:
      1. ADD    client to the newly chosen server.
      2. DELETE client from the old server.

    The preference in user_server_prefs must already be updated before calling this.
    Returns combined list of (server_name, ok, msg).
    """
    user = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if not user:
        return []

    pkg      = db.execute("SELECT * FROM packages WHERE id=?", (pkg_id,)).fetchone()
    user_pkg = db.execute(
        "SELECT * FROM user_packages WHERE user_id=? AND package_id=? AND active=1",
        (user_id, pkg_id)).fetchone()
    if not pkg or not user_pkg:
        return []

    results = []

    new_srv = db.execute("SELECT * FROM servers WHERE id=? AND is_active=1",
                         (new_server_id,)).fetchone()
    if new_srv:
        ok, msg = sync_user_to_server(db, new_srv, user, user_pkg, pkg, 'add')
        results.append((new_srv['name'], ok, msg))

    old_srv = db.execute("SELECT * FROM servers WHERE id=?",
                         (old_server_id,)).fetchone()
    if old_srv:
        ok, msg = sync_user_to_server(db, old_srv, user, {}, pkg, 'delete')
        results.append((old_srv['name'], ok, msg))

    return results


def sync_on_first_server_select(db, user, pkg_id: int, new_server_id: int) -> list:
    """
    Triggered when: user selects a preferred server for the FIRST TIME
    (previously on all servers, now only on one).

    Steps:
      1. ADD    client to the selected server.
      2. DELETE client from all OTHER servers for this package.

    The preference must already be saved before calling this.
    """
    pkg      = db.execute("SELECT * FROM packages WHERE id=?", (pkg_id,)).fetchone()
    user_pkg = db.execute(
        "SELECT * FROM user_packages WHERE user_id=? AND package_id=? AND active=1",
        (user['id'], pkg_id)).fetchone()
    if not pkg or not user_pkg:
        return []

    all_servers = _all_servers_for_pkg(db, pkg_id)
    results     = []

    for srv in all_servers:
        if srv['id'] == new_server_id:
            ok, msg = sync_user_to_server(db, srv, user, user_pkg, pkg, 'add')
        else:
            ok, msg = sync_user_to_server(db, srv, user, {}, pkg, 'delete')
        results.append((srv['name'], ok, msg))

    return results


def sync_on_user_settings_change(db, user_id: int) -> list:
    """
    Triggered when any of the following change:
      - secret_token          → password in proxy
      - stream_format         → isHttp
      - block_adult           → isAdultAllowed
      - extra_connections     → maxStreamCount
      - balance / promos      → expiredAt
        (topup confirmed, promo applied, trial granted, admin balance edit,
         daily charge deducted)

    Pushes 'update' to all servers of every active package.
    """
    user = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if not user:
        return []

    active = db.execute("""
        SELECT up.*, pk.id AS pkg_id
        FROM user_packages up
        JOIN packages pk ON up.package_id = pk.id
        WHERE up.user_id = ? AND up.active = 1
    """, (user_id,)).fetchall()

    results = []
    for up in active:
        pkg = db.execute("SELECT * FROM packages WHERE id=?", (up['pkg_id'],)).fetchone()
        if not pkg:
            continue
        res = sync_user_to_all_servers(db, user, up, pkg, 'update')
        results.extend(res)

    return results


# ── Backward-compat aliases ───────────────────────────────────────────────────

def sync_user_package_connect(db, user, pkg_id: int) -> list:
    return sync_on_package_connect(db, user, pkg_id)

def sync_user_package_disconnect(db, user, pkg_id: int) -> list:
    return sync_on_package_disconnect(db, user, pkg_id)

def sync_user_settings(db, user_id: int, changed_keys: list) -> list:
    return sync_on_user_settings_change(db, user_id)


# ── Admin helper ──────────────────────────────────────────────────────────────

def get_server_status(server) -> dict:
    """GET /config/clients from agent — returns raw data or error."""
    url = _agent_url(server) + '/config/clients'
    try:
        r = requests.get(url, headers=_headers(server), timeout=AGENT_TIMEOUT)
        if r.status_code == 200:
            return {'ok': True, 'data': r.json()}
        return {'ok': False, 'error': f'HTTP {r.status_code}'}
    except Exception as e:
        return {'ok': False, 'error': str(e)}
