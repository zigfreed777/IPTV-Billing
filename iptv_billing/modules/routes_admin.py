from flask import Blueprint, render_template, request, redirect, url_for, session, flash, jsonify
from functools import wraps
import math, secrets as _sec
from .db import get_db
from .settings import all_by_group, save as save_setting, invalidate as invalidate_settings
from .auth import log_action, csrf_token, csrf_valid, hash_password, validate_password
from .mailer import send_broadcast

bp = Blueprint('admin', __name__, url_prefix='/admin')
from .config import ADMIN_LOGINS


def _get_broadcast_recipients(db, filter_type: str, filter_value: str) -> list:
    """Return list of user rows matching the broadcast filter."""
    base = ("SELECT u.id, u.login, u.email, u.unsub_token "
            "FROM users u "
            "WHERE u.email_confirmed=1 AND u.is_blocked=0 AND u.email_news=1")
    if filter_type == 'subscribed':
        sql = (base + " AND EXISTS ("
               "SELECT 1 FROM user_packages up WHERE up.user_id=u.id AND up.active=1)")
    elif filter_type == 'expired':
        sql = (base + " AND NOT EXISTS ("
               "SELECT 1 FROM user_packages up WHERE up.user_id=u.id AND up.active=1)")
    elif filter_type == 'trial':
        sql = base + " AND u.trial_used=1"
    elif filter_type == 'no_payment':
        sql = (base + " AND NOT EXISTS ("
               "SELECT 1 FROM topups t WHERE t.user_id=u.id AND t.status='confirmed')")
    else:  # 'all'
        sql = base
    return db.execute(sql).fetchall()

def admin_required(f):
    @wraps(f)
    def decorated(*a, **kw):
        if 'user_id' not in session: return redirect(url_for('auth.login'))
        try:
            db   = get_db()
            user = db.execute("SELECT login, is_blocked FROM users WHERE id=?",
                              (session['user_id'],)).fetchone()
        except Exception:
            user = None
        if not user:
            session.clear()
            return redirect(url_for('auth.login'))
        if user['login'] not in ADMIN_LOGINS or user['is_blocked']:
            flash('Нет доступа.', 'error')
            return redirect(url_for('dashboard.index'))
        return f(*a, **kw)
    return decorated

def get_ip():
    return (request.headers.get('X-Forwarded-For') or request.remote_addr or '').split(',')[0].strip()

def _csrf_check():
    if not csrf_valid(request.form.get('_csrf','')):
        flash('Ошибка безопасности.', 'error')
        return False
    return True

# ── DASHBOARD ─────────────────────────────────────────────────────────────────
@bp.route('/')
@admin_required
def index():
    db    = get_db()
    stats = {
        'users_total':    db.execute("SELECT COUNT(*) FROM users").fetchone()[0],
        'users_active':   db.execute("SELECT COUNT(*) FROM users WHERE is_blocked=0 AND email_confirmed=1").fetchone()[0],
        'subscribed':     db.execute("SELECT COUNT(DISTINCT user_id) FROM user_packages WHERE active=1").fetchone()[0],
        'revenue_month':  db.execute("SELECT COALESCE(SUM(amount),0) FROM charges WHERE charged_at >= date('now','start of month')").fetchone()[0],
        'revenue_total':  db.execute("SELECT COALESCE(SUM(amount),0) FROM topups WHERE status='confirmed'").fetchone()[0],
        'pending_topups': db.execute("SELECT COUNT(*) FROM topups WHERE status='pending'").fetchone()[0],
        'packages_active':db.execute("SELECT COUNT(*) FROM user_packages WHERE active=1").fetchone()[0],
        'promos_active':  db.execute("SELECT COUNT(*) FROM promocodes WHERE is_active=1").fetchone()[0],
        'unread_chat':    db.execute("SELECT COUNT(*) FROM chat_messages WHERE from_admin=0 AND is_read=0").fetchone()[0],
    }
    recent_users    = db.execute("SELECT * FROM users ORDER BY registered_at DESC LIMIT 8").fetchall()
    recent_charges  = db.execute("""SELECT ch.*, u.login, pk.name as pkg_name FROM charges ch
        JOIN users u ON ch.user_id=u.id
        JOIN packages pk ON ch.package_id=pk.id
        ORDER BY ch.charged_at DESC LIMIT 8""").fetchall()
    top_users       = db.execute("""SELECT u.*, COUNT(up.id) as pkg_count,
        COALESCE(SUM(pk.price_per_day),0) as daily_cost
        FROM users u
        LEFT JOIN user_packages up ON up.user_id=u.id AND up.active=1
        LEFT JOIN packages pk ON up.package_id=pk.id
        WHERE u.is_blocked=0 AND u.email_confirmed=1
        GROUP BY u.id ORDER BY u.balance DESC LIMIT 6""").fetchall()
    pkg_popularity  = db.execute("""SELECT pk.name, s.icon, COUNT(up.id) as cnt
        FROM packages pk JOIN services s ON pk.service_id=s.id
        LEFT JOIN user_packages up ON up.package_id=pk.id AND up.active=1
        GROUP BY pk.id ORDER BY cnt DESC LIMIT 6""").fetchall()
    pending_topups_list = db.execute("""SELECT t.*, u.login FROM topups t
        JOIN users u ON t.user_id=u.id WHERE t.status='pending'
        ORDER BY t.created_at LIMIT 5""").fetchall()
    return render_template('admin/index.html', stats=stats,
                           recent_users=recent_users, recent_charges=recent_charges,
                           top_users=top_users, pkg_popularity=pkg_popularity,
                           pending_topups_list=pending_topups_list,
                           csrf=csrf_token())

# ── ANALYTICS ─────────────────────────────────────────────────────────────────
@bp.route('/analytics')
@admin_required
def analytics():
    db = get_db()
    revenue_daily = [dict(r) for r in db.execute("""SELECT date(charged_at) as d, COALESCE(SUM(amount),0) as total
        FROM charges WHERE charged_at >= date('now','-30 day')
        GROUP BY d ORDER BY d""").fetchall()]
    reg_daily = [dict(r) for r in db.execute("""SELECT date(registered_at) as d, COUNT(*) as cnt
        FROM users WHERE registered_at >= date('now','-30 day')
        GROUP BY d ORDER BY d""").fetchall()]
    tariff_stats = [dict(r) for r in db.execute("""SELECT pk.name, s.name as svc, COUNT(up.id) as cnt
        FROM packages pk
        JOIN services s ON pk.service_id=s.id
        LEFT JOIN user_packages up ON up.package_id=pk.id AND up.active=1
        GROUP BY pk.id ORDER BY cnt DESC""").fetchall()]
    topup_stats = db.execute("""SELECT method, COUNT(*) as cnt, COALESCE(SUM(amount),0) as total
        FROM topups WHERE status='confirmed' GROUP BY method""").fetchall()
    funnel = {
        'registered':  db.execute("SELECT COUNT(*) FROM users").fetchone()[0],
        'confirmed':   db.execute("SELECT COUNT(*) FROM users WHERE email_confirmed=1").fetchone()[0],
        'paid':        db.execute("SELECT COUNT(DISTINCT user_id) FROM topups WHERE status='confirmed'").fetchone()[0],
        'subscribed':  db.execute("SELECT COUNT(DISTINCT user_id) FROM user_packages WHERE active=1").fetchone()[0],
    }
    return render_template('admin/analytics.html',
                           revenue_daily=revenue_daily, reg_daily=reg_daily,
                           tariff_stats=tariff_stats, topup_stats=topup_stats,
                           funnel=funnel, csrf=csrf_token())

# ── USERS ─────────────────────────────────────────────────────────────────────
@bp.route('/users')
@admin_required
def users():
    db    = get_db()
    q     = request.args.get('q','').strip()
    page  = max(1, request.args.get('page', 1, type=int))
    filt  = request.args.get('filter','')
    per   = 25
    where, params = "1=1", []
    if q:
        where += " AND (login LIKE ? OR email LIKE ?)"
        params += [f'%{q}%', f'%{q}%']
    if filt == 'blocked':      where += " AND is_blocked=1"
    elif filt == 'unconfirmed':where += " AND email_confirmed=0"
    elif filt == 'active':     where += " AND id IN (SELECT DISTINCT user_id FROM user_packages WHERE active=1)"
    elif filt == 'expired':    where += " AND id NOT IN (SELECT DISTINCT user_id FROM user_packages WHERE active=1)"
    elif filt == 'trial':      where += " AND trial_used=1"
    elif filt == 'news_off':   where += " AND email_news=0"
    total = db.execute(f"SELECT COUNT(*) FROM users WHERE {where}", params).fetchone()[0]
    rows  = db.execute(f"""SELECT u.* FROM users u
        WHERE {where} ORDER BY u.registered_at DESC LIMIT ? OFFSET ?""",
        params + [per, (page-1)*per]).fetchall()
    return render_template('admin/users.html', users=rows, q=q, filt=filt,
                           page=page, pages=math.ceil(total/per) if total else 1,
                           total=total, csrf=csrf_token())

@bp.route('/users/<int:uid>', methods=['GET','POST'])
@admin_required
def user_detail(uid):
    db   = get_db()
    user = db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if not user:
        flash('Пользователь не найден.', 'error')
        return redirect(url_for('admin.users'))
    if request.method == 'POST':
        if not _csrf_check(): return redirect(url_for('admin.user_detail', uid=uid))
        action   = request.form.get('action')
        admin_id = session['user_id']
        if action == 'add_balance':
            try:   amount = float(request.form.get('amount',0))
            except ValueError: amount = 0
            note = request.form.get('note','')[:200]
            if 0 < amount <= 100000:
                with db:
                    db.execute("UPDATE users SET balance=balance+? WHERE id=?", (amount, uid))
                    db.execute("INSERT INTO topups (user_id,amount,method,status,note,confirmed_at) VALUES (?,?,'admin','confirmed',?,datetime('now'))",
                               (uid, amount, note))
                    log_action(db, admin_id, 'admin_topup', f'user={uid},amount={amount}', get_ip())
                flash(f'Начислено {amount:.2f} ₽.', 'success')
                try:
                    from .proxy_agent import sync_on_user_settings_change
                    sync_on_user_settings_change(db, uid)
                except Exception as _e:
                    import logging as _log
                    _log.getLogger(__name__).error(f'[proxy] add_balance: {_e}')
        elif action == 'set_balance':
            try:   amount = float(request.form.get('amount',0))
            except ValueError: amount = 0
            if 0 <= amount <= 100000:
                with db:
                    db.execute("UPDATE users SET balance=? WHERE id=?", (amount, uid))
                    log_action(db, admin_id, 'admin_set_balance', f'user={uid},bal={amount}', get_ip())
                flash(f'Баланс: {amount:.2f} ₽.', 'success')
                try:
                    from .proxy_agent import sync_on_user_settings_change
                    sync_on_user_settings_change(db, uid)
                except Exception as _e:
                    import logging as _log
                    _log.getLogger(__name__).error(f'[proxy] set_balance: {_e}')
        elif action == 'block':
            reason = request.form.get('reason','')[:200]
            with db:
                db.execute("UPDATE users SET is_blocked=1, block_reason=? WHERE id=?", (reason, uid))
                log_action(db, admin_id, 'admin_block', f'user={uid}', get_ip())
            flash('Заблокирован.', 'success')
        elif action == 'unblock':
            with db:
                db.execute("UPDATE users SET is_blocked=0, block_reason=NULL WHERE id=?", (uid,))
                log_action(db, admin_id, 'admin_unblock', f'user={uid}', get_ip())
            flash('Разблокирован.', 'success')
        elif action == 'add_package':
            pkg_id = request.form.get('package_id','')
            pkg    = db.execute("SELECT * FROM packages WHERE id=? AND is_active=1", (pkg_id,)).fetchone()
            if pkg:
                existing = db.execute("""SELECT up.id FROM user_packages up
                    JOIN packages pk ON up.package_id=pk.id
                    WHERE up.user_id=? AND up.active=1 AND pk.service_id=?""",
                    (uid, pkg['service_id'])).fetchone()
                _old_pkg_id = existing['package_id'] if existing else None
                with db:
                    if existing:
                        db.execute("UPDATE user_packages SET active=0, stopped_at=datetime('now') WHERE id=?",
                                   (existing['id'],))
                    db.execute("INSERT INTO user_packages (user_id, package_id) VALUES (?,?)", (uid, pkg_id))
                    log_action(db, admin_id, 'admin_add_package', f'user={uid},pkg={pkg["name"]}', get_ip())
                flash(f'Пакет «{pkg["name"]}» подключён.', 'success')
                try:
                    from .proxy_agent import sync_on_package_disconnect, sync_on_package_connect
                    _fresh = db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
                    if _old_pkg_id:
                        sync_on_package_disconnect(db, _fresh, int(_old_pkg_id))
                    sync_on_package_connect(db, _fresh, int(pkg_id))
                except Exception as _e:
                    import logging as _log
                    _log.getLogger(__name__).error(f'[proxy] admin_add_package: {_e}')
        elif action == 'set_comment':
            with db: db.execute("UPDATE users SET comment=? WHERE id=?",
                                (request.form.get('comment','')[:500], uid))
            flash('Комментарий сохранён.', 'success')
        elif action == 'set_password':
            pwd = request.form.get('password','')
            if validate_password(pwd):
                with db:
                    db.execute("UPDATE users SET password_hash=? WHERE id=?", (hash_password(pwd), uid))
                    log_action(db, admin_id, 'admin_reset_pwd', f'user={uid}', get_ip())
                flash('Пароль изменён.', 'success')
        return redirect(url_for('admin.user_detail', uid=uid))
    active_pkgs = db.execute("""SELECT up.id as up_id, up.started_at, pk.name, pk.price_per_day,
        pk.connections, s.name as svc_name, s.icon as svc_icon
        FROM user_packages up JOIN packages pk ON up.package_id=pk.id
        JOIN services s ON pk.service_id=s.id
        WHERE up.user_id=? AND up.active=1 ORDER BY s.sort_order""", (uid,)).fetchall()
    topups   = db.execute("SELECT * FROM topups WHERE user_id=? ORDER BY created_at DESC LIMIT 20", (uid,)).fetchall()
    charges  = db.execute("""SELECT ch.*, pk.name as pkg_name FROM charges ch
        JOIN packages pk ON ch.package_id=pk.id
        WHERE ch.user_id=? ORDER BY ch.charged_at DESC LIMIT 30""", (uid,)).fetchall()
    audit    = db.execute("SELECT * FROM audit_log WHERE user_id=? ORDER BY created_at DESC LIMIT 30", (uid,)).fetchall()
    referrals= db.execute("""SELECT u.login, r.created_at FROM referrals r
        JOIN users u ON r.referred_id=u.id WHERE r.referrer_id=?""", (uid,)).fetchall()
    promo_uses = db.execute("""SELECT pu.*, p.code FROM promocode_uses pu
        JOIN promocodes p ON pu.promocode_id=p.id WHERE pu.user_id=?""", (uid,)).fetchall()
    chat_count = db.execute("SELECT COUNT(*) FROM chat_messages WHERE user_id=?", (uid,)).fetchone()[0]
    all_packages = db.execute("SELECT pk.*, s.name as svc_name FROM packages pk JOIN services s ON pk.service_id=s.id WHERE pk.is_active=1 ORDER BY s.sort_order, pk.sort_order").fetchall()
    return render_template('admin/user_detail.html', user=user, all_packages=all_packages,
                           active_pkgs=active_pkgs, topups=topups, charges=charges,
                           audit=audit, referrals=referrals, promo_uses=promo_uses,
                           chat_count=chat_count, csrf=csrf_token())

# ── TOPUPS ────────────────────────────────────────────────────────────────────
@bp.route('/topups')
@admin_required
def topups():
    db   = get_db()
    rows = db.execute("""SELECT tp.*, u.login, u.email FROM topups tp
        JOIN users u ON tp.user_id=u.id
        WHERE tp.status='pending' ORDER BY tp.created_at""").fetchall()
    return render_template('admin/topups.html', topups=rows, csrf=csrf_token())

@bp.route('/topups/<int:tid>/confirm', methods=['POST'])
@admin_required
def confirm_topup(tid):
    if not _csrf_check(): return redirect(url_for('admin.topups'))
    db    = get_db()
    topup = db.execute("SELECT * FROM topups WHERE id=? AND status='pending'", (tid,)).fetchone()
    if not topup:
        flash('Не найден или уже обработан.', 'error')
        return redirect(url_for('admin.topups'))
    with db:
        db.execute("UPDATE topups SET status='confirmed', confirmed_at=datetime('now') WHERE id=?", (tid,))
        db.execute("UPDATE users SET balance=balance+? WHERE id=?", (topup['amount'], topup['user_id']))
        log_action(db, session['user_id'], 'admin_topup_confirmed',
                   f'topup={tid},amount={topup["amount"]}', get_ip())
    # Topup changes balance → update expiredAt on proxy
    try:
        from .proxy_agent import sync_on_user_settings_change
        sync_on_user_settings_change(db, topup['user_id'])
    except Exception as _e:
        import logging as _log
        _log.getLogger(__name__).error(f'[proxy] confirm_topup: {_e}')
    flash(f'Пополнение #{tid} подтверждено.', 'success')
    return redirect(url_for('admin.topups'))

@bp.route('/topups/<int:tid>/reject', methods=['POST'])
@admin_required
def reject_topup(tid):
    if not _csrf_check(): return redirect(url_for('admin.topups'))
    db = get_db()
    with db:
        db.execute("UPDATE topups SET status='rejected' WHERE id=? AND status='pending'", (tid,))
        log_action(db, session['user_id'], 'admin_topup_rejected', f'topup={tid}', get_ip())
    flash(f'Пополнение #{tid} отклонено.', 'info')
    return redirect(url_for('admin.topups'))

# ── TARIFFS ───────────────────────────────────────────────────────────────────
@bp.route('/tariffs')
@admin_required
def tariffs():
    # Тарифы заменены на Услуги и Пакеты
    return redirect(url_for('admin.services'))

# ── PROMOCODES ────────────────────────────────────────────────────────────────
@bp.route('/promocodes', methods=['GET','POST'])
@admin_required
def promocodes():
    db = get_db()
    if request.method == 'POST':
        if not _csrf_check(): return redirect(url_for('admin.promocodes'))
        action = request.form.get('action')
        if action == 'add':
            code      = request.form.get('code','').strip().upper()[:32]
            ptype     = request.form.get('type','percent')
            try:     value = float(request.form.get('value',0))
            except:  value = 0
            max_uses  = int(request.form.get('max_uses',1) or 1)
            package_id = request.form.get('package_id') or None
            valid_until = request.form.get('valid_until') or None
            comment   = request.form.get('comment','').strip()[:200]
            if not code or value <= 0 or ptype not in ('percent','fixed','days','trial'):
                flash('Заполните поля корректно.', 'error')
            elif db.execute("SELECT 1 FROM promocodes WHERE code=?", (code,)).fetchone():
                flash('Такой промокод уже существует.', 'error')
            else:
                with db: db.execute("""INSERT INTO promocodes
                    (code,type,value,max_uses,package_id,valid_until,comment)
                    VALUES (?,?,?,?,?,?,?)""",
                    (code, ptype, value, max_uses, package_id, valid_until, comment))
                flash(f'Промокод {code} создан.', 'success')
        elif action == 'toggle':
            with db: db.execute("UPDATE promocodes SET is_active=NOT is_active WHERE id=?",
                                (request.form.get('promo_id'),))
        elif action == 'delete':
            pid = request.form.get('promo_id')
            with db:
                db.execute("DELETE FROM promocode_uses WHERE promocode_id=?", (pid,))
                db.execute("DELETE FROM promocodes WHERE id=?", (pid,))
            flash('Промокод удалён.', 'success')
        return redirect(url_for('admin.promocodes'))
    rows     = db.execute("""SELECT p.*, pk.name as pkg_name FROM promocodes p
        LEFT JOIN packages pk ON p.package_id=pk.id ORDER BY p.created_at DESC""").fetchall()
    packages = db.execute("SELECT pk.*, s.name as svc_name FROM packages pk JOIN services s ON pk.service_id=s.id WHERE pk.is_active=1").fetchall()
    return render_template('admin/promocodes.html', promocodes=rows, packages=packages, csrf=csrf_token())

# ── NOTIFY SETTINGS ───────────────────────────────────────────────────────────
@bp.route('/notify-settings')
@admin_required
def notify_settings():
    return redirect(url_for('admin.communications', tab='notify'))


@bp.route('/broadcasts')
@admin_required
def broadcasts():
    return redirect(url_for('admin.communications', tab='broadcasts'))

@bp.route('/broadcasts/<int:bid>', methods=['GET', 'POST'])
@admin_required
def broadcast_detail(bid):
    db  = get_db()
    bc  = db.execute("SELECT * FROM broadcasts WHERE id=?", (bid,)).fetchone()
    if not bc:
        flash('Рассылка не найдена.', 'error')
        return redirect(url_for('admin.communications', tab='broadcasts'))
    if request.method == 'POST':
        if not _csrf_check(): return redirect(url_for('admin.broadcast_detail', bid=bid))
        action = request.form.get('action')
        if action == 'send' and bc['status'] == 'draft':
            recipients = _get_broadcast_recipients(db, bc['filter_type'], bc['filter_value'])
            sent = 0
            for r in recipients:
                token = r['unsub_token'] or _sec.token_urlsafe(24)
                if not r['unsub_token']:
                    with db:
                        db.execute("UPDATE users SET unsub_token=? WHERE id=?", (token, r['id']))
                ok = send_broadcast(r['email'], bc['subject'], bc['body_html'], token)
                if ok: sent += 1
            with db:
                db.execute("UPDATE broadcasts SET status='sent', sent_count=?, sent_at=datetime('now') WHERE id=?",
                           (sent, bid))
                log_action(db, session['user_id'], 'broadcast_sent', f'id={bid},sent={sent}', get_ip())
            flash(f'Рассылка отправлена: {sent} писем.', 'success')
            return redirect(url_for('admin.communications', tab='broadcasts'))
        elif action == 'delete' and bc['status'] == 'draft':
            with db: db.execute("DELETE FROM broadcasts WHERE id=?", (bid,))
            flash('Черновик удалён.', 'info')
            return redirect(url_for('admin.communications', tab='broadcasts'))
    recipients_count = len(_get_broadcast_recipients(db, bc['filter_type'], bc['filter_value']))
    return render_template('admin/broadcast_detail.html', bc=bc,
                           recipients_count=recipients_count, csrf=csrf_token())


@bp.route('/broadcasts/preview-count', methods=['POST'])
@admin_required
def broadcast_preview_count():
    db  = get_db()
    ft  = request.form.get('filter_type','all')
    fv  = request.form.get('filter_value','')
    cnt = len(_get_broadcast_recipients(db, ft, fv))
    return jsonify({'count': cnt})

# ── SMTP SETTINGS ────────────────────────────────────────────────────────────
@bp.route('/smtp')
@admin_required
def smtp_settings():
    return redirect(url_for('admin.communications'))

@bp.route('/communications', methods=['GET', 'POST'])
@admin_required
def communications():
    db   = get_db()
    cfg  = db.execute("SELECT * FROM smtp_settings WHERE id=1").fetchone()
    tab  = request.args.get('tab', 'smtp')
    test_result = None
    if request.method == 'POST':
        if not _csrf_check(): return redirect(url_for('admin.communications'))
        action = request.form.get('action')
        if action == 'save_smtp':
            host      = request.form.get('host', '').strip()
            port      = int(request.form.get('port', 465) or 465)
            user      = request.form.get('user', '').strip()
            password  = request.form.get('password', '').strip()
            from_name = request.form.get('from_name', 'IPTV Billing').strip()[:60]
            site_url  = request.form.get('site_url', '').strip().rstrip('/')
            enabled   = 1 if request.form.get('enabled') else 0
            if not password and cfg: password = cfg['password']
            with db:
                db.execute("""UPDATE smtp_settings SET host=?,port=?,user=?,password=?,
                    from_name=?,site_url=?,enabled=?,updated_at=datetime('now') WHERE id=1""",
                    (host,port,user,password,from_name,site_url,enabled))
            flash('Настройки SMTP сохранены.', 'success')
            return redirect(url_for('admin.communications', tab='smtp'))
        elif action == 'test_smtp':
            from .mailer import send_test
            test_to = request.form.get('test_to','').strip()
            if not test_to:
                flash('Укажите адрес для теста.', 'error')
            else:
                ok = send_test(test_to)
                test_result = ('success', f'Письмо отправлено на {test_to}') if ok else (
                              'error', 'Ошибка. Проверьте настройки и логи.')
            cfg = db.execute("SELECT * FROM smtp_settings WHERE id=1").fetchone()
            tab = 'smtp'
        elif action == 'create_broadcast':
            subject      = request.form.get('subject','').strip()[:200]
            body_html    = request.form.get('body_html','').strip()
            filter_type  = request.form.get('filter_type','all')
            filter_value = request.form.get('filter_value','').strip()
            if not subject or not body_html:
                flash('Заполните тему и текст.', 'error')
            else:
                with db:
                    cur = db.execute("""INSERT INTO broadcasts
                        (subject,body_html,filter_type,filter_value,status,created_by)
                        VALUES (?,?,?,?,'draft',?)""",
                        (subject,body_html,filter_type,filter_value,session['user_id']))
                flash(f'Рассылка #{cur.lastrowid} создана.', 'success')
                return redirect(url_for('admin.communications', tab='broadcasts'))
            tab = 'broadcasts'
        elif action == 'save_notify':
            enabled      = 1 if request.form.get('notify_enabled') else 0
            days_before  = request.form.get('notif_days_before','7,3,1').strip()
            from .settings import save as ss
            ss('notif_enabled', str(enabled))
            ss('notif_days_before', days_before)
            with db:
                db.execute("""UPDATE notify_settings SET enabled=?,days_before=?,updated_at=datetime('now') WHERE id=1""",
                           (enabled, days_before))
            flash('Настройки уведомлений сохранены.', 'success')
            return redirect(url_for('admin.communications', tab='notify'))
        return redirect(url_for('admin.communications', tab=tab))
    broadcasts = db.execute("""SELECT b.*,u.login as author FROM broadcasts b
        LEFT JOIN users u ON b.created_by=u.id ORDER BY b.created_at DESC""").fetchall()
    notify_cfg = db.execute("SELECT * FROM notify_settings WHERE id=1").fetchone()
    notify_log = db.execute("""SELECT nl.*,u.login FROM notification_log nl
        JOIN users u ON nl.user_id=u.id ORDER BY nl.sent_at DESC LIMIT 30""").fetchall()
    return render_template('admin/communications.html', cfg=cfg, tab=tab,
                           broadcasts=broadcasts, notify_cfg=notify_cfg,
                           notify_log=notify_log, test_result=test_result, csrf=csrf_token())

# ── ADMIN CHAT ────────────────────────────────────────────────────────────────
@bp.route('/chat')
@admin_required
def chat():
    db   = get_db()
    # Get all users who have sent at least one message, ordered by unread first
    threads = db.execute("""
        SELECT u.id, u.login, u.email,
               COUNT(CASE WHEN m.from_admin=0 AND m.is_read=0 THEN 1 END) as unread,
               MAX(m.created_at) as last_msg,
               (SELECT body FROM chat_messages WHERE user_id=u.id ORDER BY created_at DESC LIMIT 1) as preview
        FROM users u
        JOIN chat_messages m ON m.user_id = u.id
        GROUP BY u.id ORDER BY unread DESC, last_msg DESC""").fetchall()
    return render_template('admin/chat.html', threads=threads, csrf=csrf_token())

@bp.route('/chat/<int:uid>', methods=['GET','POST'])
@admin_required
def chat_thread(uid):
    db   = get_db()
    user = db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if not user:
        flash('Пользователь не найден.', 'error')
        return redirect(url_for('admin.chat'))
    if request.method == 'POST':
        if not _csrf_check(): return redirect(url_for('admin.chat_thread', uid=uid))
        body = request.form.get('body','').strip()[:2000]
        if body:
            with db:
                db.execute("INSERT INTO chat_messages (user_id, from_admin, body) VALUES (?,1,?)",
                           (uid, body))
        return redirect(url_for('admin.chat_thread', uid=uid))
    with db:
        db.execute("UPDATE chat_messages SET is_read=1 WHERE user_id=? AND from_admin=0", (uid,))
    messages = db.execute("SELECT * FROM chat_messages WHERE user_id=? ORDER BY created_at ASC",
                          (uid,)).fetchall()
    return render_template('admin/chat_thread.html', user=user, messages=messages, csrf=csrf_token())

@bp.route('/chat/broadcast', methods=['POST'])
@admin_required
def chat_broadcast():
    if not _csrf_check(): return redirect(url_for('admin.chat'))
    body  = request.form.get('body','').strip()[:2000]
    filt  = request.form.get('filter','all')
    if not body:
        flash('Сообщение не может быть пустым.', 'error')
        return redirect(url_for('admin.chat'))
    db = get_db()
    if filt == 'active':
        users = db.execute("""SELECT DISTINCT u.id FROM users u
            JOIN user_packages up ON up.user_id=u.id WHERE up.active=1""").fetchall()
    else:
        users = db.execute("SELECT id FROM users WHERE email_confirmed=1 AND is_blocked=0").fetchall()
    with db:
        for u in users:
            db.execute("INSERT INTO chat_messages (user_id, from_admin, body) VALUES (?,1,?)",
                       (u['id'], body))
    flash(f'Сообщение отправлено {len(users)} пользователям.', 'success')
    return redirect(url_for('admin.chat'))

# ── SERVICES & PACKAGES ──────────────────────────────────────────────────────
@bp.route('/services', methods=['GET', 'POST'])
@admin_required
def services():
    db = get_db()
    if request.method == 'POST':
        if not _csrf_check(): return redirect(url_for('admin.services'))
        action = request.form.get('action','')
        # ── Services ─────────────────────────────────────────────────────────
        if action == 'add_service':
            name = request.form.get('name','').strip()[:60]
            desc = request.form.get('description','').strip()[:200]
            icon = request.form.get('icon','📦').strip()[:8]
            if name:
                with db: db.execute("INSERT INTO services (name,description,icon) VALUES (?,?,?)", (name,desc,icon))
                flash('Услуга добавлена.', 'success')
            else: flash('Введите название.', 'error')
        elif action == 'edit_service':
            sid  = request.form.get('service_id')
            name = request.form.get('name','').strip()[:60]
            desc = request.form.get('description','').strip()[:200]
            icon = request.form.get('icon','📦').strip()[:8]
            with db: db.execute("UPDATE services SET name=?,description=?,icon=? WHERE id=?", (name,desc,icon,sid))
            flash('Услуга обновлена.', 'success')
        elif action == 'toggle_service':
            with db: db.execute("UPDATE services SET is_active=NOT is_active WHERE id=?",
                                (request.form.get('service_id'),))
        elif action == 'delete_service':
            sid = request.form.get('service_id')
            # only if no active packages
            cnt = db.execute("SELECT COUNT(*) FROM packages WHERE service_id=? AND is_active=1",(sid,)).fetchone()[0]
            if cnt: flash('Нельзя удалить услугу с активными пакетами.', 'error')
            else:
                with db: db.execute("DELETE FROM services WHERE id=?", (sid,))
                flash('Услуга удалена.', 'success')
        # ── Packages ─────────────────────────────────────────────────────────
        elif action == 'add_package':
            try:
                svc_id = int(request.form.get('service_id',0))
                name   = request.form.get('name','').strip()[:60]
                desc   = request.form.get('description','').strip()[:200]
                price  = float(request.form.get('price_per_day',0))
                conns  = int(request.form.get('connections',1))
                allow_extra = 1 if request.form.get("allow_extra_connections") else 0
                extra_price = float(request.form.get("extra_connection_price") or 0)
                max_extra   = int(request.form.get("max_extra_connections") or 5)
                if name and price > 0 and svc_id:
                    with db: db.execute(
                        """INSERT INTO packages (service_id,name,description,price_per_day,connections,
                            allow_extra_connections,extra_connection_price,max_extra_connections)
                            VALUES (?,?,?,?,?,?,?,?)""",
                        (svc_id,name,desc,price,conns,allow_extra,extra_price,max_extra))
                    flash('Пакет добавлен.', 'success')
                else: flash('Заполните все поля.', 'error')
            except (ValueError,TypeError): flash('Некорректные данные.', 'error')
        elif action == 'edit_package':
            pid  = request.form.get('package_id')
            try:
                name  = request.form.get('name','').strip()[:60]
                desc  = request.form.get('description','').strip()[:200]
                price = float(request.form.get('price_per_day',0))
                conns = int(request.form.get('connections',1))
                allow_extra = 1 if request.form.get("allow_extra_connections") else 0
                extra_price = float(request.form.get("extra_connection_price") or 0)
                max_extra   = int(request.form.get("max_extra_connections") or 5)
                with db: db.execute(
                    """UPDATE packages SET name=?,description=?,price_per_day=?,connections=?,
                        allow_extra_connections=?,extra_connection_price=?,max_extra_connections=?
                        WHERE id=?""",
                    (name,desc,price,conns,allow_extra,extra_price,max_extra,pid))
                flash('Пакет обновлён.', 'success')
            except (ValueError,TypeError): flash('Некорректные данные.', 'error')
        elif action == 'toggle_package':
            with db: db.execute("UPDATE packages SET is_active=NOT is_active WHERE id=?",
                                (request.form.get('package_id'),))
        elif action == 'delete_package':
            pid = request.form.get('package_id')
            cnt = db.execute("SELECT COUNT(*) FROM user_packages WHERE package_id=? AND active=1",(pid,)).fetchone()[0]
            if cnt: flash(f'Нельзя удалить — пакет активен у {cnt} пользователей.', 'error')
            else:
                with db:
                    db.execute("DELETE FROM servers WHERE package_id=?", (pid,))
                    db.execute("DELETE FROM packages WHERE id=?", (pid,))
                flash('Пакет удалён.', 'success')
        # ── Servers ───────────────────────────────────────────────────────────
        elif action == 'add_server':
            try:
                pkg_id     = int(request.form.get('package_id',0))
                name       = request.form.get('name','').strip()[:60]
                ip         = request.form.get('ip','').strip()[:100]
                proxy_port = int(request.form.get('proxy_port',8080))
                api_port   = int(request.form.get('api_port',4444))
                comment    = request.form.get('comment','').strip()[:200]
                api_token = request.form.get("api_token","").strip()
                if name and ip and pkg_id:
                    with db: db.execute("INSERT INTO servers (package_id,name,ip,proxy_port,api_port,comment,api_token) VALUES (?,?,?,?,?,?,?)",
                                        (pkg_id,name,ip,proxy_port,api_port,comment,api_token))
                    flash('Сервер добавлен.', 'success')
                else: flash('Заполните обязательные поля.', 'error')
            except (ValueError,TypeError): flash('Некорректные данные.', 'error')
        elif action == 'edit_server':
            sid        = request.form.get('server_id')
            try:
                name       = request.form.get('name','').strip()[:60]
                ip         = request.form.get('ip','').strip()[:100]
                proxy_port = int(request.form.get('proxy_port',8080))
                api_port   = int(request.form.get('api_port',4444))
                comment    = request.form.get('comment','').strip()[:200]
                api_token = request.form.get("api_token","").strip()
                with db: db.execute("UPDATE servers SET name=?,ip=?,proxy_port=?,api_port=?,comment=?,api_token=? WHERE id=?",
                                    (name,ip,proxy_port,api_port,comment,api_token,sid))
                flash('Сервер обновлён.', 'success')
            except (ValueError,TypeError): flash('Некорректные данные.', 'error')
        elif action == 'toggle_server':
            with db: db.execute("UPDATE servers SET is_active=NOT is_active WHERE id=?",
                                (request.form.get('server_id'),))
        elif action == 'delete_server':
            sid = request.form.get('server_id')
            with db:
                db.execute("DELETE FROM user_server_prefs WHERE server_id=?", (sid,))
                db.execute("DELETE FROM servers WHERE id=?", (sid,))
            flash('Сервер удалён.', 'success')
        return redirect(url_for('admin.services'))

    svcs     = db.execute("SELECT * FROM services ORDER BY sort_order, id").fetchall()
    pkgs     = db.execute("""SELECT pk.*, s.name as svc_name, s.icon as svc_icon
        FROM packages pk JOIN services s ON pk.service_id=s.id
        ORDER BY s.sort_order, pk.sort_order""").fetchall()
    servers  = db.execute("""SELECT sv.*, pk.name as pkg_name
        FROM servers sv JOIN packages pk ON sv.package_id=pk.id
        ORDER BY pk.sort_order, sv.sort_order, sv.id""").fetchall()
    pkg_stats = {r['package_id']: r['cnt'] for r in db.execute(
        "SELECT package_id, COUNT(*) as cnt FROM user_packages WHERE active=1 GROUP BY package_id").fetchall()}
    return render_template('admin/services.html', services=svcs, packages=pkgs,
                           servers=servers, pkg_stats=pkg_stats, csrf=csrf_token())


@bp.route('/settings', methods=['GET', 'POST'])
@admin_required
def settings():
    db = get_db()
    if request.method == 'POST':
        if not _csrf_check(): return redirect(url_for('admin.settings'))
        changed = 0
        for key, value in request.form.items():
            if key.startswith('_'): continue
            # Normalize booleans: unchecked checkboxes aren't sent at all,
            # so we pre-set all bool keys to 0 before processing form
        # First reset all bool settings to 0
        bool_keys = [r['key'] for r in db.execute(
            "SELECT key FROM site_settings WHERE type='bool'").fetchall()]
        for k in bool_keys:
            if k not in request.form:
                save_setting(k, '0')
                changed += 1
        # Then save all submitted values
        for key, value in request.form.items():
            if key.startswith('_'): continue
            value = value.strip()
            save_setting(key, value)
            changed += 1
        invalidate_settings()
        log_action(db, session['user_id'], 'settings_saved', f'changed={changed}', get_ip())
        flash(f'Настройки сохранены ({changed} параметров).', 'success')
        return redirect(url_for('admin.settings'))
    groups = all_by_group()
    packages = db.execute("SELECT id, name FROM packages WHERE is_active=1 ORDER BY sort_order").fetchall()
    tab = request.args.get('tab', 'brand')
    return render_template('admin/settings.html', groups=groups, packages=packages,
                           tab=tab, csrf=csrf_token())
