"""Background scheduler — daemon thread.
Tasks:
  check_expiring_subs()  every 6h — low balance warning
  auto_renew_packages()  every 1h — placeholder (daily_charge handles it)
  daily_charge()         every 6h — posуточное списание за активные пакеты
"""
import threading, time, logging, sqlite3, secrets as _sec
from datetime import datetime, timedelta
from .db import DB_PATH
from . import mailer

log = logging.getLogger(__name__)
_started = False

def _conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c

def _get_settings(db):
    return db.execute("SELECT * FROM notify_settings WHERE id=1").fetchone()

def _ensure_unsub_token(db, user_id):
    row = db.execute("SELECT unsub_token FROM users WHERE id=?", (user_id,)).fetchone()
    if row and row['unsub_token']: return row['unsub_token']
    token = _sec.token_urlsafe(24)
    db.execute("UPDATE users SET unsub_token=? WHERE id=?", (token, user_id))
    db.commit()
    return token

def daily_charge():
    """Charge each active package once per calendar day."""
    log.info('[scheduler] daily_charge start')
    db = _conn()
    try:
        users = db.execute("""
            SELECT u.id, u.balance,
                GROUP_CONCAT(up.id)            as up_ids,
                GROUP_CONCAT(up.package_id)    as pkg_ids,
                GROUP_CONCAT(pk.price_per_day) as prices,
                GROUP_CONCAT(pk.name)          as names
            FROM users u
            JOIN (SELECT * FROM user_packages ORDER BY id) up ON up.user_id = u.id
            JOIN packages pk ON up.package_id = pk.id
            WHERE up.active = 1 AND u.is_blocked = 0
            GROUP BY u.id""").fetchall()
        charged = 0
        for u in users:
            total = sum(float(p) for p in u['prices'].split(','))
            if total <= 0: continue
            already = db.execute("""SELECT 1 FROM charges
                WHERE user_id=? AND charged_at >= date('now')""",
                (u['id'],)).fetchone()
            if already: continue
            if u['balance'] < total:
                log.warning(f'[scheduler] low balance uid={u["id"]} need={total:.2f} have={u["balance"]:.2f}')
                db.execute("""UPDATE user_packages SET active=0, stopped_at=datetime('now')
                    WHERE user_id=? AND active=1""", (u['id'],))
                db.commit()
                # Packages deactivated — remove user from all proxy servers
                try:
                    from .proxy_agent import sync_on_package_disconnect
                    _deact_user = db.execute("SELECT * FROM users WHERE id=?", (u['id'],)).fetchone()
                    for _pid in u['pkg_ids'].split(','):
                        sync_on_package_disconnect(db, _deact_user, int(_pid))
                except Exception as _pe:
                    log.error(f'[scheduler] proxy deactivate uid={u["id"]}: {_pe}')
                continue
            try:
                db.execute("UPDATE users SET balance=balance-? WHERE id=?", (total, u['id']))
                for pid, price in zip(u['pkg_ids'].split(','), u['prices'].split(',')):
                    db.execute("INSERT INTO charges (user_id,package_id,amount) VALUES (?,?,?)",
                               (u['id'], int(pid), float(price)))
                db.commit()
                charged += 1
                # Balance decreased — update expiredAt on proxy
                try:
                    from .proxy_agent import sync_on_user_settings_change
                    sync_on_user_settings_change(db, u['id'])
                except Exception as _pe:
                    log.error(f'[scheduler] proxy sync after charge uid={u["id"]}: {_pe}')
            except Exception as e:
                db.rollback()
                log.error(f'[scheduler] charge failed uid={u["id"]}: {e}')
        log.info(f'[scheduler] daily_charge done charged={charged}')
    finally:
        db.close()

def check_low_balance():
    """Notify users whose balance covers < 3 days of current packages."""
    log.info('[scheduler] check_low_balance start')
    db = _conn()
    try:
        cfg = _get_settings(db)
        if not cfg or not cfg['enabled']: return
        rows = db.execute("""
            SELECT u.id, u.login, u.email, u.balance, u.email_news, u.unsub_token,
                COALESCE(SUM(pk.price_per_day),0) as daily
            FROM users u
            JOIN user_packages up ON up.user_id=u.id
            JOIN packages pk ON up.package_id=pk.id
            WHERE up.active=1 AND u.email_confirmed=1 AND u.is_blocked=0 AND u.email_news=1
            GROUP BY u.id
            HAVING u.balance < daily * 3 AND daily > 0""").fetchall()
        sent = 0
        for u in rows:
            already = db.execute("""SELECT 1 FROM notification_log
                WHERE user_id=? AND type='low_balance'
                AND sent_at >= date('now','-1 day')""", (u['id'],)).fetchone()
            if already: continue
            token = _ensure_unsub_token(db, u['id'])
            days_left = int(u['balance'] / u['daily']) if u['daily'] > 0 else 0
            ok = mailer.send_sub_expiring(
                u['email'], u['login'], days_left, '', token)
            if ok:
                db.execute("INSERT INTO notification_log (user_id,type,days_before) VALUES (?,?,?)",
                           (u['id'], 'low_balance', days_left))
                db.commit()
                sent += 1
        log.info(f'[scheduler] check_low_balance done sent={sent}')
    finally:
        db.close()

def _loop():
    INTERVAL_CHARGE = 6 * 3600
    INTERVAL_NOTIFY = 6 * 3600
    last_charge = 0
    last_notify = 0
    while True:
        now = time.monotonic()
        try:
            if now - last_charge >= INTERVAL_CHARGE:
                daily_charge()
                last_charge = now
            if now - last_notify >= INTERVAL_NOTIFY:
                check_low_balance()
                last_notify = now
        except Exception as e:
            log.error(f'[scheduler] error: {e}')
        time.sleep(60)

def start():
    global _started
    if _started: return
    _started = True
    t = threading.Thread(target=_loop, daemon=True, name='scheduler')
    t.start()
    log.info('[scheduler] started')
