from flask import Blueprint, render_template, request, redirect, url_for, session, flash, jsonify
from functools import wraps
from datetime import datetime, timedelta
import secrets as _sec
from .db import get_db
from .settings import get_int as sint
from .auth import (hash_password, verify_password, validate_password,
                   log_action, csrf_token, csrf_valid)
from .mailer import send_confirm

bp = Blueprint('dashboard', __name__)

def login_required(f):
    @wraps(f)
    def decorated(*a, **kw):
        if 'user_id' not in session:
            return redirect(url_for('auth.login'))
        user = get_user()
        if user is None:
            session.clear()
            flash('Сессия устарела. Войдите снова.', 'warning')
            return redirect(url_for('auth.login'))
        if user['is_blocked']:
            session.clear()
            flash('Аккаунт заблокирован.', 'error')
            return redirect(url_for('auth.login'))
        return f(*a, **kw)
    return decorated

def get_user(db=None):
    if db is None: db = get_db()
    try:
        return db.execute(
            "SELECT * FROM users WHERE id=?", (session['user_id'],)).fetchone()
    except Exception:
        return None

def get_ip():
    return (request.headers.get('X-Forwarded-For') or request.remote_addr or '').split(',')[0].strip()

def csrf_check():
    if not csrf_valid(request.form.get('_csrf', '')):
        flash('Ошибка безопасности. Попробуйте снова.', 'error')
        return False
    return True

def _user_connections(db, user_id):
    """Total simultaneous connections allowed across all active packages."""
    row = db.execute("""
        SELECT COALESCE(SUM(pk.connections), 0) as total
        FROM user_packages up JOIN packages pk ON up.package_id = pk.id
        WHERE up.user_id = ? AND up.active = 1""", (user_id,)).fetchone()
    return row['total'] if row else 0

def _daily_cost(db, user_id):
    """Total daily cost of all active packages."""
    row = db.execute("""
        SELECT COALESCE(SUM(pk.price_per_day), 0) as total
        FROM user_packages up JOIN packages pk ON up.package_id = pk.id
        WHERE up.user_id = ? AND up.active = 1""", (user_id,)).fetchone()
    return round(row['total'], 2) if row else 0.0

def _active_packages(db, user_id):
    return db.execute("""
        SELECT up.id as up_id, up.extra_connections, up.started_at,
               pk.*, s.name as service_name, s.icon as service_icon
        FROM user_packages up
        JOIN packages pk ON up.package_id = pk.id
        JOIN services s ON pk.service_id = s.id
        WHERE up.user_id = ? AND up.active = 1
        ORDER BY s.sort_order, pk.price_per_day""", (user_id,)).fetchall()

# ── OVERVIEW ──────────────────────────────────────────────────────────────────
@bp.route('/dashboard')
@login_required
def index():
    db      = get_db()
    user    = get_user(db)
    pkgs    = _active_packages(db, user['id'])
    daily   = _daily_cost(db, user['id'])
    conns   = _user_connections(db, user['id'])
    charges = db.execute("""SELECT ch.*, pk.name as pkg_name FROM charges ch
        JOIN packages pk ON ch.package_id = pk.id
        WHERE ch.user_id = ? ORDER BY ch.charged_at DESC LIMIT 5""", (user['id'],)).fetchall()
    referrals = db.execute("""SELECT u.login, u.registered_at
        FROM referrals r JOIN users u ON r.referred_id = u.id
        WHERE r.referrer_id = ? ORDER BY r.created_at DESC""", (user['id'],)).fetchall()
    unread_chat = db.execute("""SELECT COUNT(*) FROM chat_messages
        WHERE user_id = ? AND from_admin = 1 AND is_read = 0""", (user['id'],)).fetchone()[0]
    return render_template('dashboard.html', user=user, packages=pkgs,
                           daily_cost=daily, connections=conns, charges=charges,
                           referrals=referrals, unread_chat=unread_chat, csrf=csrf_token())

# ── SERVICES / PACKAGES ───────────────────────────────────────────────────────
@bp.route('/dashboard/services')
@login_required
def services():
    db      = get_db()
    user    = get_user(db)
    active  = _active_packages(db, user['id'])
    active_ids = {r['package_id'] for r in db.execute(
        "SELECT package_id FROM user_packages WHERE user_id=? AND active=1",
        (user['id'],)).fetchall()}
    services_list = db.execute(
        "SELECT * FROM services WHERE is_active=1 ORDER BY sort_order").fetchall()
    all_packages  = db.execute("""
        SELECT pk.*, s.name as service_name, s.icon as service_icon
        FROM packages pk JOIN services s ON pk.service_id = s.id
        WHERE pk.is_active = 1 ORDER BY s.sort_order, pk.price_per_day, pk.sort_order""").fetchall()
    charges = db.execute("""SELECT ch.*, pk.name as pkg_name, s.name as svc_name
        FROM charges ch
        JOIN packages pk ON ch.package_id = pk.id
        JOIN services s ON pk.service_id = s.id
        WHERE ch.user_id = ? ORDER BY ch.charged_at DESC LIMIT 30""", (user['id'],)).fetchall()
    daily   = _daily_cost(db, user['id'])
    return render_template('services.html', user=user,
                           active_packages=active, active_ids=active_ids,
                           services=services_list, all_packages=all_packages,
                           charges=charges, daily_cost=daily, csrf=csrf_token())

@bp.route('/dashboard/package/connect/<int:pkg_id>', methods=['POST'])
@login_required
def package_connect(pkg_id):
    if not csrf_check(): return redirect(url_for('dashboard.services'))
    db   = get_db()
    user = get_user(db)
    pkg  = db.execute("SELECT pk.*, s.id as service_id FROM packages pk JOIN services s ON pk.service_id=s.id WHERE pk.id=? AND pk.is_active=1",
                      (pkg_id,)).fetchone()
    if not pkg:
        flash('Пакет не найден.', 'error')
        return redirect(url_for('dashboard.services'))
    # Validate promo code if provided
    promo_code = request.form.get('promo_code', '').strip().upper()
    promo      = None
    promo_id   = None
    discount   = 0.0
    if promo_code:
        promo = db.execute("""SELECT * FROM promocodes
            WHERE code=? AND is_active=1
            AND (valid_from IS NULL OR valid_from <= datetime('now'))
            AND (valid_until IS NULL OR valid_until >= datetime('now'))
            AND (max_uses=0 OR uses < max_uses)
            AND (package_id IS NULL OR package_id=?)""",
            (promo_code, pkg_id)).fetchone()
        if not promo:
            flash('Промокод не найден или недействителен.', 'error')
            return redirect(url_for('dashboard.services'))
        already = db.execute("SELECT 1 FROM promocode_uses WHERE promocode_id=? AND user_id=?",
                             (promo['id'], user['id'])).fetchone()
        if already:
            flash('Промокод уже использован.', 'error')
            return redirect(url_for('dashboard.services'))
        promo_id = promo['id']
        if promo['type'] == 'percent':
            discount = round(pkg['price_per_day'] * promo['value'] / 100, 2)
        elif promo['type'] == 'fixed':
            discount = min(promo['value'], pkg['price_per_day'])
    final_price = max(0, pkg['price_per_day'] - discount)
    # Check if user has another package from the same service — replace it
    existing = db.execute("""SELECT up.id, up.package_id FROM user_packages up
        JOIN packages pk ON up.package_id = pk.id
        WHERE up.user_id = ? AND up.active = 1 AND pk.service_id = ?""",
        (user['id'], pkg['service_id'])).fetchone()
    # Check balance covers at least one day
    if user['balance'] < final_price:
        flash(f'Недостаточно средств. Нужно минимум {final_price:.2f} ₽ (стоимость 1 дня).', 'error')
        return redirect(url_for('dashboard.services'))
    # Remember old package_id before deactivation (for proxy disconnect)
    old_pkg_id = existing['package_id'] if existing else None
    with db:
        if existing:
            db.execute("UPDATE user_packages SET active=0, stopped_at=datetime('now') WHERE id=?",
                       (existing['id'],))
        db.execute("INSERT INTO user_packages (user_id, package_id) VALUES (?,?)",
                   (user['id'], pkg_id))
        if promo_id:
            db.execute("UPDATE promocodes SET uses=uses+1 WHERE id=?", (promo_id,))
            db.execute("INSERT INTO promocode_uses (promocode_id,user_id,discount_amount) VALUES (?,?,?)",
                       (promo_id, user['id'], discount))
        log_action(db, user['id'], 'package_connected',
                   f'pkg={pkg["name"]},promo={promo_code or "-"}', get_ip())
    # Sync to proxy: disconnect old package first, then connect new one
    try:
        from .proxy_agent import sync_on_package_disconnect, sync_on_package_connect
        if old_pkg_id:
            sync_on_package_disconnect(db, user, old_pkg_id)
        sync_on_package_connect(db, user, pkg_id)
    except Exception as e:
        import logging; logging.getLogger(__name__).error(f'[proxy] connect: {e}')
    flash(f'Пакет «{pkg["name"]}» подключён.', 'success')
    return redirect(url_for('dashboard.services'))

@bp.route('/dashboard/package/disconnect/<int:up_id>', methods=['POST'])
@login_required
def package_disconnect(up_id):
    if not csrf_check(): return redirect(url_for('dashboard.services'))
    db   = get_db()
    user = get_user(db)
    row  = db.execute("SELECT up.*, pk.name as pkg_name FROM user_packages up JOIN packages pk ON up.package_id=pk.id WHERE up.id=? AND up.user_id=? AND up.active=1",
                      (up_id, user['id'])).fetchone()
    if not row:
        flash('Пакет не найден.', 'error')
        return redirect(url_for('dashboard.services'))
    pkg_id = row['package_id']
    with db:
        db.execute("UPDATE user_packages SET active=0, stopped_at=datetime('now') WHERE id=?", (up_id,))
        log_action(db, user['id'], 'package_disconnected', f'pkg={row["pkg_name"]}', get_ip())
    # Sync to proxy server(s)
    try:
        from .proxy_agent import sync_user_package_disconnect
        sync_user_package_disconnect(db, user, pkg_id)
    except Exception as e:
        import logging; logging.getLogger(__name__).error(f'[proxy] disconnect: {e}')
    flash(f'Пакет «{row["pkg_name"]}» отключён.', 'success')
    return redirect(url_for('dashboard.services'))

# ── FINANCES ──────────────────────────────────────────────────────────────────
@bp.route('/dashboard/finances', methods=['GET', 'POST'])
@login_required
def finances():
    db   = get_db()
    user = get_user(db)
    if request.method == 'POST':
        if not csrf_check(): return redirect(url_for('dashboard.finances'))
        action = request.form.get('action', '')
        if action == 'topup':
            try:    amount = float(request.form.get('amount', 0))
            except: amount = 0
            method = request.form.get('method', 'card')
            min_t = sint('min_topup', 10)
            max_t = sint('max_topup', 50000)
            if amount < min_t:
                flash(f'Минимум: {min_t} ₽.', 'error')
            elif amount > max_t:
                flash(f'Максимум: {max_t:,} ₽.', 'error')
            elif method not in ('card', 'crypto', 'transfer'):
                flash('Неверный способ оплаты.', 'error')
            else:
                ext_id = _sec.token_urlsafe(12)
                with db:
                    cur = db.execute("INSERT INTO topups (user_id,amount,method,status,external_id) VALUES (?,?,?,'pending',?)",
                                     (user['id'], amount, method, ext_id))
                    log_action(db, user['id'], 'topup_created', f'amount={amount},method={method}', get_ip())
                flash(f'Заявка #{cur.lastrowid} на {amount:.0f} ₽ создана.', 'info')
                return redirect(url_for('dashboard.finances'))
        elif action == 'cancel_topup':
            tid = request.form.get('topup_id', '')
            with db:
                db.execute("UPDATE topups SET status='cancelled' WHERE id=? AND user_id=? AND status='pending'",
                           (tid, user['id']))
            flash('Заявка отменена.', 'info')
        return redirect(url_for('dashboard.finances'))
    topups  = db.execute("SELECT * FROM topups WHERE user_id=? ORDER BY created_at DESC LIMIT 30",
                         (user['id'],)).fetchall()
    charges = db.execute("""SELECT ch.*, pk.name as pkg_name FROM charges ch
        JOIN packages pk ON ch.package_id = pk.id
        WHERE ch.user_id = ? ORDER BY ch.charged_at DESC LIMIT 50""", (user['id'],)).fetchall()
    return render_template('finances.html', user=user, topups=topups,
                           charges=charges, csrf=csrf_token())

# ── MEDIA / ACCESS ────────────────────────────────────────────────────────────
@bp.route('/dashboard/media', methods=['GET', 'POST'])
@login_required
def media():
    db   = get_db()
    user = get_user(db)
    if request.method == 'POST':
        if not csrf_check(): return redirect(url_for('dashboard.media'))
        action = request.form.get('action', '')
        if action == 'regen_token':
            from .auth import gen_secret_token
            new_token = gen_secret_token()
            with db:
                db.execute("UPDATE users SET secret_token=?, updated_at=datetime('now') WHERE id=?",
                           (new_token, user['id']))
                log_action(db, user['id'], 'token_regenerated', ip=get_ip())
            # Sync new token (password) to proxy server(s)
            try:
                from .proxy_agent import sync_user_settings
                # Refresh user after token change
                user = get_user(db)
                sync_user_settings(db, user['id'], ['secret_token'])
            except Exception as e:
                import logging; logging.getLogger(__name__).error(f'[proxy] regen_token: {e}')
            flash('Токен обновлён. Обновите плейлисты на всех устройствах.', 'success')
        elif action == 'update_media':
            fmt         = request.form.get('stream_format', 'ts')
            block_adult = 1 if request.form.get('block_adult') else 0
            if fmt not in ('ts', 'm3u8'): fmt = 'ts'
            with db:
                db.execute("UPDATE users SET stream_format=?, block_adult=?, updated_at=datetime('now') WHERE id=?",
                           (fmt, block_adult, user['id']))
            flash('Настройки медиа сохранены.', 'success')
            try:
                from .proxy_agent import sync_user_settings
                sync_user_settings(db, user['id'], ['stream_format','block_adult'])
            except Exception as e:
                import logging; logging.getLogger(__name__).error(f'[proxy] media update: {e}')
        elif action == 'set_server':
            pkg_id    = request.form.get('pkg_id', type=int)
            server_id = request.form.get('server_id', type=int)
            if pkg_id and server_id:
                # Verify server belongs to this package
                srv = db.execute("SELECT id FROM servers WHERE id=? AND package_id=? AND is_active=1",
                                 (server_id, pkg_id)).fetchone()
                if srv:
                    # Read old server preference BEFORE overwriting it
                    _old_pref = db.execute(
                        "SELECT server_id FROM user_server_prefs WHERE user_id=? AND package_id=?",
                        (user['id'], pkg_id)).fetchone()
                    _old_server_id = _old_pref['server_id'] if _old_pref else None
                    with db:
                        db.execute("""INSERT INTO user_server_prefs (user_id,package_id,server_id,updated_at)
                            VALUES (?,?,?,datetime('now'))
                            ON CONFLICT(user_id,package_id) DO UPDATE SET server_id=excluded.server_id,
                            updated_at=excluded.updated_at""",
                            (user['id'], pkg_id, server_id))
                    # ADD on new server, DELETE from others
                    try:
                        from .proxy_agent import sync_on_server_change, sync_on_first_server_select
                        if _old_server_id and _old_server_id != server_id:
                            # Had a preference before — swap servers
                            sync_on_server_change(db, user['id'], pkg_id, _old_server_id, server_id)
                        else:
                            # First-time selection — add to chosen, delete from all others
                            sync_on_first_server_select(db, user, pkg_id, server_id)
                    except Exception as e:
                        import logging; logging.getLogger(__name__).error(f'[proxy] set_server: {e}')
                    flash('Сервер выбран.', 'success')
        return redirect(url_for('dashboard.media'))
    smtp      = db.execute("SELECT site_url FROM smtp_settings WHERE id=1").fetchone()
    site_url  = smtp['site_url'] if smtp else 'http://localhost:5003'
    # Load active packages with their available servers and current server pref
    active_pkgs = db.execute("""SELECT up.id as up_id, pk.id as pkg_id, pk.name, s.name as svc_name, s.icon as svc_icon
        FROM user_packages up JOIN packages pk ON up.package_id=pk.id
        JOIN services s ON pk.service_id=s.id
        WHERE up.user_id=? AND up.active=1 ORDER BY s.sort_order, pk.sort_order""",
        (user['id'],)).fetchall()
    pkg_servers = {}
    pkg_selected = {}
    for p in active_pkgs:
        pkg_servers[p['pkg_id']] = db.execute(
            "SELECT * FROM servers WHERE package_id=? AND is_active=1 ORDER BY sort_order, id",
            (p['pkg_id'],)).fetchall()
        pref = db.execute("SELECT server_id FROM user_server_prefs WHERE user_id=? AND package_id=?",
                          (user['id'], p['pkg_id'])).fetchone()
        pkg_selected[p['pkg_id']] = pref['server_id'] if pref else None
    return render_template('media.html', user=user, site_url=site_url,
                           active_pkgs=active_pkgs, pkg_servers=pkg_servers,
                           pkg_selected=pkg_selected, csrf=csrf_token())

# ── PROFILE ───────────────────────────────────────────────────────────────────
@bp.route('/dashboard/profile', methods=['GET', 'POST'])
@login_required
def profile():
    db   = get_db()
    user = get_user(db)
    if request.method == 'POST':
        if not csrf_check(): return redirect(url_for('dashboard.profile'))
        action = request.form.get('action')
        if action == 'change_password':
            old, new, cfm = (request.form.get(k, '') for k in ('old_password', 'new_password', 'confirm_password'))
            if not verify_password(old, user['password_hash']):
                flash('Неверный текущий пароль.', 'error')
            elif not validate_password(new):
                flash('Пароль: минимум 6 символов.', 'error')
            elif new != cfm:
                flash('Пароли не совпадают.', 'error')
            else:
                with db:
                    db.execute("UPDATE users SET password_hash=?, updated_at=datetime('now') WHERE id=?",
                               (hash_password(new), user['id']))
                    log_action(db, user['id'], 'password_changed', ip=get_ip())
                flash('Пароль изменён.', 'success')
        elif action == 'update_info':
            phone     = request.form.get('phone', '').strip()[:30]
            birthdate = request.form.get('birthdate', '').strip()[:10]
            tz        = request.form.get('timezone', 'UTC')
            lang      = request.form.get('language', 'ru')
            email_news = 1 if request.form.get('email_news') else 0
            auto_renew = 1 if request.form.get('auto_renew') else 0
            allowed_tz = {'UTC','Europe/Moscow','Europe/Kiev','Europe/Minsk',
                          'Asia/Almaty','Asia/Tashkent','Asia/Yekaterinburg'}
            if tz not in allowed_tz: tz = 'UTC'
            if lang not in ('ru', 'en', 'uk'): lang = 'ru'
            with db:
                db.execute("""UPDATE users SET phone=?, birthdate=?, timezone=?, language=?,
                    email_news=?, auto_renew=?, updated_at=datetime('now') WHERE id=?""",
                    (phone or None, birthdate or None, tz, lang, email_news, auto_renew, user['id']))
            flash('Профиль обновлён.', 'success')
        return redirect(url_for('dashboard.profile'))
    audit = db.execute("SELECT * FROM audit_log WHERE user_id=? ORDER BY created_at DESC LIMIT 15",
                       (user['id'],)).fetchall()
    return render_template('profile.html', user=user, audit=audit, csrf=csrf_token())

# ── CHAT ──────────────────────────────────────────────────────────────────────
@bp.route('/dashboard/chat', methods=['GET', 'POST'])
@login_required
def chat():
    db   = get_db()
    user = get_user(db)
    if request.method == 'POST':
        if not csrf_check(): return redirect(url_for('dashboard.chat'))
        body = request.form.get('body', '').strip()[:2000]
        if body:
            with db:
                db.execute("INSERT INTO chat_messages (user_id, from_admin, body) VALUES (?,0,?)",
                           (user['id'], body))
        return redirect(url_for('dashboard.chat'))
    with db:
        db.execute("UPDATE chat_messages SET is_read=1 WHERE user_id=? AND from_admin=1",
                   (user['id'],))
    messages = db.execute("""SELECT * FROM chat_messages WHERE user_id=?
        ORDER BY created_at ASC LIMIT 200""", (user['id'],)).fetchall()
    return render_template('chat.html', user=user, messages=messages, csrf=csrf_token())

@bp.route('/dashboard/chat/poll')
@login_required
def chat_poll():
    db   = get_db()
    user = get_user(db)
    after = request.args.get('after', '1970-01-01')
    msgs = db.execute("""SELECT id, from_admin, body, created_at FROM chat_messages
        WHERE user_id=? AND created_at > ? ORDER BY created_at ASC LIMIT 50""",
        (user['id'], after)).fetchall()
    with db:
        db.execute("UPDATE chat_messages SET is_read=1 WHERE user_id=? AND from_admin=1 AND is_read=0",
                   (user['id'],))
    return jsonify([dict(m) for m in msgs])

# ── RESEND CONFIRM ────────────────────────────────────────────────────────────
@bp.route('/dashboard/resend_confirm', methods=['POST'])
@login_required
def resend_confirm():
    if not csrf_valid(request.form.get('_csrf', '')):
        flash('Ошибка безопасности.', 'error')
        return redirect(url_for('dashboard.index'))
    db   = get_db()
    user = db.execute("SELECT * FROM users WHERE id=?", (session['user_id'],)).fetchone()
    if user['email_confirmed']:
        flash('Email уже подтверждён.', 'info')
    else:
        from .auth import gen_token
        token = gen_token()
        with db:
            db.execute("UPDATE users SET confirm_token=?, confirm_sent_at=datetime('now') WHERE id=?",
                       (token, user['id']))
        send_confirm(user['email'], user['login'], token)
        flash('Письмо отправлено.', 'success')
    return redirect(url_for('dashboard.index'))

# ── UNSUBSCRIBE ───────────────────────────────────────────────────────────────
@bp.route('/unsubscribe/<token>')
def unsubscribe(token):
    db   = get_db()
    user = db.execute("SELECT * FROM users WHERE unsub_token=?", (token,)).fetchone()
    if not user:
        return render_template('unsubscribe.html', status='invalid', token='', csrf=csrf_token())
    with db:
        db.execute("UPDATE users SET email_news=0 WHERE id=?", (user['id'],))
    return render_template('unsubscribe.html', status='ok', login=user['login'],
                           token=token, csrf=csrf_token())

@bp.route('/resubscribe', methods=['POST'])
def resubscribe():
    if not csrf_valid(request.form.get('_csrf', '')):
        return redirect(url_for('auth.login'))
    token = request.form.get('token', '')
    db    = get_db()
    with db:
        db.execute("UPDATE users SET email_news=1 WHERE unsub_token=?", (token,))
    flash('Вы снова подписаны на рассылку.', 'success')
    return redirect(url_for('auth.login'))

@bp.route('/dashboard/package/extra-connections/<int:up_id>', methods=['POST'])
@login_required
def package_extra_connections(up_id):
    if not csrf_check(): return redirect(url_for('dashboard.services'))
    db   = get_db()
    user = get_user(db)
    row  = db.execute("""SELECT up.*, pk.connections, pk.allow_extra_connections,
        pk.extra_connection_price, pk.max_extra_connections, pk.name as pkg_name,
        pk.price_per_day
        FROM user_packages up JOIN packages pk ON up.package_id=pk.id
        WHERE up.id=? AND up.user_id=? AND up.active=1""", (up_id, user['id'])).fetchone()
    if not row or not row['allow_extra_connections']:
        flash('Дополнительные подключения недоступны для этого пакета.', 'error')
        return redirect(url_for('dashboard.services'))
    try:
        extra = max(0, min(int(request.form.get('extra', 0)), row['max_extra_connections']))
    except (ValueError, TypeError):
        extra = 0
    old_extra = row['extra_connections'] or 0
    diff  = extra - old_extra
    cost  = round(diff * row['extra_connection_price'], 2)
    if diff > 0 and user['balance'] < cost:
        flash(f'Недостаточно средств: нужно {cost:.2f} ₽.', 'error')
        return redirect(url_for('dashboard.services'))
    with db:
        db.execute("UPDATE user_packages SET extra_connections=? WHERE id=?", (extra, up_id))
        if diff > 0 and cost > 0:
            # Deduct one-time activation cost for extra connections
            db.execute("UPDATE users SET balance=balance-? WHERE id=?", (cost, user['id']))
        if diff != 0:
            log_action(db, user['id'], 'extra_connections_changed',
                       f'pkg={row["pkg_name"]},extra={extra},cost={cost}', get_ip())
    try:
        from .proxy_agent import sync_user_settings
        sync_user_settings(db, user['id'], ['extra_connections'])
    except Exception as e:
        import logging; logging.getLogger(__name__).error(f'[proxy] extra_conns: {e}')
    flash(f'Дополнительные подключения: {extra}.', 'success')
    return redirect(url_for('dashboard.services'))

@bp.route('/dashboard/promo/check', methods=['POST'])
@login_required
def promo_check():
    from flask import jsonify
    if not csrf_valid(request.form.get('_csrf', '')): return jsonify({'ok': False, 'error': 'csrf'})
    code      = request.form.get('code', '').strip().upper()
    tariff_id = request.form.get('tariff_id', type=int)
    db   = get_db()
    user = get_user(db)
    if not code: return jsonify({'ok': False, 'error': 'empty'})
    promo = db.execute("""SELECT * FROM promocodes
        WHERE code=? AND is_active=1
        AND (valid_from IS NULL OR valid_from <= datetime('now'))
        AND (valid_until IS NULL OR valid_until >= datetime('now'))
        AND (max_uses=0 OR uses < max_uses)
        AND (package_id IS NULL OR package_id=?)""",
        (code, tariff_id)).fetchone()
    if not promo:
        return jsonify({'ok': False, 'error': 'Промокод не найден или недействителен'})
    already = db.execute("SELECT 1 FROM promocode_uses WHERE promocode_id=? AND user_id=?",
                          (promo['id'], user['id'])).fetchone()
    if already:
        return jsonify({'ok': False, 'error': 'Промокод уже использован'})
    t = promo['type']
    if t == 'percent':  desc = f'−{promo["value"]:.0f}%'
    elif t == 'fixed':  desc = f'−{promo["value"]:.0f} ₽'
    elif t == 'days':   desc = f'+{int(promo["value"])} дней бесплатно'
    else:               desc = 'Промокод действителен'
    return jsonify({'ok': True, 'type': t, 'value': promo['value'], 'desc': desc})
