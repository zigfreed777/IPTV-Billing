from flask import Blueprint, render_template, request, redirect, url_for, session, flash
from datetime import datetime, timedelta
import secrets as _sec
from .db import get_db
from .auth import (hash_password, verify_password, gen_secret_token, gen_referral_code,
                   gen_token, validate_email, validate_login, validate_password,
                   log_action, rate_limit, csrf_token, csrf_valid)
from .settings import get as sget, get_bool as sbool, get_int as sint
from .mailer import send_confirm, send_reset, send_welcome, send_trial_activated
from .config import ADMIN_LOGINS

bp = Blueprint('auth', __name__)

def get_ip():
    return (request.headers.get('X-Forwarded-For') or request.remote_addr or '').split(',')[0].strip()

def _csrf_check():
    if not csrf_valid(request.form.get('_csrf', '')):
        flash('Ошибка безопасности. Попробуйте снова.', 'error')
        return False
    return True

@bp.route('/register', methods=['GET', 'POST'])
def register():
    if 'user_id' in session: return redirect(url_for('dashboard.index'))
    if not sbool('reg_enabled', True):
        flash('Регистрация временно закрыта.', 'warning')
        return redirect(url_for('auth.login'))
    ref_code  = request.args.get('ref', '')
    if sbool('reg_invite_only', False) and not ref_code and request.method == 'GET':
        flash('Регистрация доступна только по реферальной ссылке.', 'warning')
        return redirect(url_for('auth.login'))
    form_data = {}
    if request.method == 'POST':
        if not _csrf_check():
            return render_template('register.html', ref_code=ref_code, csrf=csrf_token())
        ip = get_ip()
        if not rate_limit(f'reg:{ip}', 5, 3600):
            flash('Слишком много попыток. Подождите час.', 'error')
            return render_template('register.html', ref_code=ref_code, csrf=csrf_token())
        login    = request.form.get('login', '').strip()
        email    = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        confirm  = request.form.get('confirm', '')
        ref_code = request.form.get('ref_code', '').strip().upper()
        form_data = {'login': login, 'email': email, 'ref_code': ref_code}
        errors = []
        if not validate_login(login):      errors.append('Логин: 4-32 символа, буквы/цифры/_')
        if not validate_email(email):      errors.append('Некорректный email')
        if not validate_password(password): errors.append('Пароль: минимум 6 символов')
        if password != confirm:            errors.append('Пароли не совпадают')
        if not errors:
            db = get_db()
            if db.execute("SELECT 1 FROM users WHERE login=?", (login,)).fetchone():
                errors.append('Логин уже занят')
            if db.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
                errors.append('Email уже зарегистрирован')
        if errors:
            for e in errors: flash(e, 'error')
            return render_template('register.html', ref_code=ref_code, form=form_data, csrf=csrf_token())
        db            = get_db()
        confirm_token = gen_token()
        secret_token  = gen_secret_token()
        ref_code_new  = gen_referral_code()
        unsub_token   = _sec.token_urlsafe(24)
        referred_by   = None
        is_admin      = login in ADMIN_LOGINS
        if ref_code:
            row = db.execute("SELECT id FROM users WHERE referral_code=?", (ref_code,)).fetchone()
            if row: referred_by = row['id']
        with db:
            db.execute("""INSERT INTO users
                (login, password_hash, email, email_confirmed, confirm_token, confirm_sent_at,
                 secret_token, referral_code, referred_by, unsub_token)
                VALUES (?,?,?,?,?,datetime('now'),?,?,?,?)""",
                (login, hash_password(password), email,
                 1 if is_admin else 0,          # ← admin auto-confirmed
                 None if is_admin else confirm_token,
                 secret_token, ref_code_new, referred_by, unsub_token))
        if is_admin:
            flash(f'Аккаунт администратора создан. Войдите в кабинет.', 'success')
        else:
            send_confirm(email, login, confirm_token)
            flash('Письмо с подтверждением отправлено. Проверьте почту.', 'success')
        return redirect(url_for('auth.login'))
    return render_template('register.html', ref_code=ref_code, form=form_data, csrf=csrf_token())

@bp.route('/confirm/<token>')
def confirm(token):
    db   = get_db()
    user = db.execute("SELECT * FROM users WHERE confirm_token=?", (token,)).fetchone()
    if not user:
        flash('Ссылка недействительна или уже использована.', 'error')
        return redirect(url_for('auth.login'))
    if user['email_confirmed']:
        flash('Email уже подтверждён.', 'info')
        return redirect(url_for('auth.login'))
    trial_days    = sint("trial_days", 3)
    trial_enabled = sbool("trial_enabled", True)
    with db:
        db.execute("UPDATE users SET email_confirmed=1, confirm_token=NULL WHERE id=?", (user['id'],))
        if user['referred_by']:
            db.execute("INSERT OR IGNORE INTO referrals (referrer_id, referred_id) VALUES (?,?)",
                       (user['referred_by'], user['id']))
        if trial_enabled and trial_days > 0 and not user['trial_used']:
            end = (datetime.utcnow() + timedelta(days=trial_days)).isoformat()
            pkg_id = sint("trial_package_id", 1)
            db.execute("UPDATE users SET trial_used=1 WHERE id=?", (user['id'],))
            db.execute("INSERT OR IGNORE INTO user_packages (user_id, package_id) VALUES (?,?)",
                       (user['id'], pkg_id))
        log_action(db, user['id'], 'email_confirmed', ip=get_ip())
    if trial_enabled and trial_days > 0 and not user['trial_used']:
        send_trial_activated(user['email'], user['login'], trial_days, user['secret_token'])
        # Sync trial package to proxy server(s)
        try:
            from .proxy_agent import sync_on_package_connect
            fresh_user = db.execute("SELECT * FROM users WHERE id=?", (user['id'],)).fetchone()
            pkg_id_trial = sint("trial_package_id", 1)
            sync_on_package_connect(db, fresh_user, pkg_id_trial)
        except Exception as _e:
            import logging as _log
            _log.getLogger(__name__).error(f'[proxy] trial connect: {_e}')
    else:
        send_welcome(user['email'], user['login'], user['secret_token'])
    flash('Email подтверждён! Войдите в аккаунт.', 'success')
    return redirect(url_for('auth.login'))

@bp.route('/login', methods=['GET', 'POST'])
def login():
    if 'user_id' in session: return redirect(url_for('dashboard.index'))
    if request.method == 'POST':
        if not _csrf_check():
            return render_template('login.html', csrf=csrf_token())
        ip        = get_ip()
        login_val = request.form.get('login', '').strip()
        password  = request.form.get('password', '')
        if not rate_limit(f'login:{ip}', 10, 300):
            flash('Слишком много попыток. Подождите 5 минут.', 'error')
            return render_template('login.html', csrf=csrf_token())
        db   = get_db()
        user = db.execute("SELECT * FROM users WHERE login=? OR email=?",
                          (login_val, login_val.lower())).fetchone()
        if not user or not verify_password(password, user['password_hash']):
            flash('Неверный логин или пароль.', 'error')
            return render_template('login.html', csrf=csrf_token())
        if not user['email_confirmed']:
            flash('Сначала подтвердите email.', 'warning')
            return render_template('login.html', csrf=csrf_token())
        if user['is_blocked']:
            flash(f'Аккаунт заблокирован: {user["block_reason"] or "обратитесь в поддержку"}', 'error')
            return render_template('login.html', csrf=csrf_token())
        if not user['password_hash'].startswith('pbkdf2:'):
            with db: db.execute("UPDATE users SET password_hash=? WHERE id=?",
                                (hash_password(password), user['id']))
        session.clear()
        session['user_id'] = user['id']
        session['login']   = user['login']
        with db:
            db.execute("UPDATE users SET last_login=datetime('now'), last_ip=? WHERE id=?", (ip, user['id']))
            log_action(db, user['id'], 'login', ip=ip)
        return redirect(url_for('dashboard.index'))
    return render_template('login.html', csrf=csrf_token())

@bp.route('/logout')
def logout():
    if 'user_id' in session:
        db = get_db()
        with db: log_action(db, session['user_id'], 'logout', ip=get_ip())
    session.clear()
    return redirect(url_for('auth.login'))

@bp.route('/forgot', methods=['GET', 'POST'])
def forgot():
    if request.method == 'POST':
        if not _csrf_check(): return render_template('forgot.html', csrf=csrf_token())
        ip    = get_ip()
        email = request.form.get('email', '').strip().lower()
        if not rate_limit(f'forgot:{ip}', 3, 3600):
            flash('Слишком много запросов. Подождите час.', 'error')
            return render_template('forgot.html', csrf=csrf_token())
        db   = get_db()
        user = db.execute("SELECT * FROM users WHERE email=? AND email_confirmed=1", (email,)).fetchone()
        if user:
            token = gen_token()
            exp   = (datetime.utcnow() + timedelta(hours=1)).isoformat()
            with db: db.execute("UPDATE users SET reset_token=?, reset_token_exp=? WHERE id=?",
                                (token, exp, user['id']))
            send_reset(email, user['login'], token)
        flash('Если email найден — письмо отправлено.', 'info')
        return redirect(url_for('auth.login'))
    return render_template('forgot.html', csrf=csrf_token())

@bp.route('/reset/<token>', methods=['GET', 'POST'])
def reset(token):
    db   = get_db()
    user = db.execute("SELECT * FROM users WHERE reset_token=?", (token,)).fetchone()
    if not user or (user['reset_token_exp'] and
                    datetime.fromisoformat(user['reset_token_exp']) < datetime.utcnow()):
        flash('Ссылка недействительна или истекла.', 'error')
        return redirect(url_for('auth.forgot'))
    if request.method == 'POST':
        if not _csrf_check(): return render_template('reset.html', token=token, csrf=csrf_token())
        pwd, confirm = request.form.get('password', ''), request.form.get('confirm', '')
        if not validate_password(pwd):
            flash('Пароль минимум 6 символов.', 'error')
            return render_template('reset.html', token=token, csrf=csrf_token())
        if pwd != confirm:
            flash('Пароли не совпадают.', 'error')
            return render_template('reset.html', token=token, csrf=csrf_token())
        with db:
            db.execute("UPDATE users SET password_hash=?, reset_token=NULL, reset_token_exp=NULL WHERE id=?",
                       (hash_password(pwd), user['id']))
            log_action(db, user['id'], 'password_reset', ip=get_ip())
        flash('Пароль изменён. Войдите.', 'success')
        return redirect(url_for('auth.login'))
    return render_template('reset.html', token=token, csrf=csrf_token())
