import smtplib, logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

log = logging.getLogger(__name__)

def _cfg():
    """Load SMTP config from DB, fall back to defaults."""
    try:
        from .db import DB_PATH
        import sqlite3
        c = sqlite3.connect(DB_PATH)
        c.row_factory = sqlite3.Row
        row = c.execute("SELECT * FROM smtp_settings WHERE id=1").fetchone()
        c.close()
        if row: return dict(row)
    except Exception as e:
        log.warning(f'[SMTP cfg] {e}')
    return {'host':'smtp.gmail.com','port':587,'user':'','password':'',
            'from_name':'IPTV Billing','site_url':'http://localhost:5003','enabled':1}

def _send(to: str, subject: str, html: str) -> bool:
    cfg  = _cfg()
    if not cfg.get('enabled') or not cfg.get('user'):
        log.warning(f'[MAIL] disabled or unconfigured, skip to={to}')
        return False
    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From']    = f'{cfg["from_name"]} <{cfg["user"]}>'
    msg['To']      = to
    msg.attach(MIMEText(html, 'html', 'utf-8'))
    host    = cfg['host']
    port    = int(cfg['port'])
    use_ssl = port == 465
    try:
        if use_ssl:
            import ssl as _ssl
            ctx = _ssl.create_default_context()
            with smtplib.SMTP_SSL(host, port, timeout=20, context=ctx) as s:
                s.ehlo()
                s.login(cfg['user'], cfg['password'])
                s.sendmail(cfg['user'], to, msg.as_string())
        else:
            with smtplib.SMTP(host, port, timeout=20) as s:
                s.ehlo()
                s.starttls()
                s.ehlo()
                s.login(cfg['user'], cfg['password'])
                s.sendmail(cfg['user'], to, msg.as_string())
        log.info(f'[MAIL] sent to={to} subj={subject[:40]}')
        return True
    except smtplib.SMTPAuthenticationError as e:
        log.error(f'[MAIL] AUTH FAILED to={to}: {e}')
        return False
    except (TimeoutError, OSError) as e:
        log.error(f'[MAIL] CONNECTION FAILED host={host}:{port} to={to}: {e}')
        return False
    except Exception as e:
        log.error(f'[MAIL] ERROR to={to}: {type(e).__name__}: {e}')
        return False

def get_site_url() -> str:
    return _cfg().get('site_url', 'http://localhost:5003').rstrip('/')

def _btn(url, label, color='#7C3AED'):
    return (f'<a href="{url}" style="display:inline-block;padding:14px 32px;background:{color};'
            f'color:#fff;font-family:sans-serif;font-size:15px;font-weight:600;'
            f'text-decoration:none;border-radius:8px;margin:16px 0">{label}</a>')

def _wrap(title, body, unsub_url=None):
    footer_unsub = (f'<p style="margin-top:8px"><a href="{unsub_url}" '
                    f'style="color:#4a5568;font-size:11px">Отписаться от рассылки</a></p>'
                    if unsub_url else '')
    t_block = f"<h2 style='color:#a78bfa;margin:0 0 16px'>{title}</h2>" if title else ''
    return f'''<!DOCTYPE html><html><body style="margin:0;padding:0;background:#0f0f1a;font-family:sans-serif">
<table width="100%" cellpadding="0" cellspacing="0"><tr><td align="center" style="padding:40px 20px">
<table width="560" style="background:#1a1a2e;border-radius:16px;overflow:hidden">
<tr><td style="background:linear-gradient(135deg,#7C3AED,#3B82F6);padding:32px;text-align:center">
<span style="color:#fff;font-size:22px;font-weight:700">📺 IPTV Billing</span></td></tr>
<tr><td style="padding:40px 32px;color:#e2e8f0">
{t_block}{body}
<p style="color:#64748b;font-size:12px;margin-top:32px;border-top:1px solid #2d2d4e;padding-top:16px">
Это автоматическое письмо, не отвечайте на него.</p>
{footer_unsub}</td></tr></table></td></tr></table></body></html>'''

def send_confirm(to, login, token):
    url = f'{get_site_url()}/confirm/{token}'
    return _send(to, '✅ Подтвердите email — IPTV Billing', _wrap(
        f'Привет, {login}!',
        f'<p>Нажмите кнопку ниже для активации аккаунта:</p>{_btn(url,"Подтвердить email")}'
        f'<p style="color:#64748b;font-size:13px">Или вставьте ссылку в браузер:<br>'
        f'<a href="{url}" style="color:#7C3AED">{url}</a></p>'
    ))

def send_reset(to, login, token):
    url = f'{get_site_url()}/reset/{token}'
    return _send(to, '🔑 Сброс пароля — IPTV Billing', _wrap(
        'Сброс пароля',
        f'<p>Запрос на сброс пароля для аккаунта <b>{login}</b>.</p>'
        f'{_btn(url,"Сбросить пароль")}'
        f'<p style="color:#64748b;font-size:13px">Ссылка действует 1 час.</p>'
    ))

def send_welcome(to, login, secret_token):
    return _send(to, '🎉 Добро пожаловать в IPTV Billing!', _wrap(
        f'Добро пожаловать, {login}!',
        f'<p>Ваш аккаунт активирован. Ваш токен для подключения устройств:</p>'
        f'<div style="background:#0f0f1a;border:1px solid #7C3AED;border-radius:8px;padding:16px;'
        f'text-align:center;font-family:monospace;font-size:28px;letter-spacing:8px;'
        f'color:#a78bfa;font-weight:700">{secret_token}</div>'
        f'<p style="color:#64748b;font-size:13px">Храните токен в безопасном месте!</p>'
        f'{_btn(get_site_url()+"/dashboard","Перейти в кабинет")}'
    ))

def send_sub_expiring(to, login, days_left, sub_end, unsub_token):
    uns = f'{get_site_url()}/unsubscribe/{unsub_token}'
    if days_left == 0:
        title, msg, color = '⚠️ Подписка истекла!', 'Ваша подписка <b>истекла</b>. Пополните баланс.', '#dc2626'
    elif days_left == 1:
        title, msg, color = '⚠️ Подписка истекает завтра!', f'Подписка истекает <b>завтра ({sub_end[:10]})</b>.', '#f59e0b'
    else:
        title, msg, color = f'⏰ Подписка через {days_left} дн.', f'Подписка истекает <b>{sub_end[:10]}</b>.', '#3b82f6'
    return _send(to, f'{title} — IPTV Billing', _wrap(
        f'Привет, {login}!',
        f'<p>{msg}</p>{_btn(get_site_url()+"/dashboard/topup","Пополнить баланс",color)}',
        unsub_url=uns
    ))

def send_auto_renewed(to, login, tariff_name, end_date, unsub_token):
    uns = f'{get_site_url()}/unsubscribe/{unsub_token}'
    return _send(to, '✅ Подписка продлена — IPTV Billing', _wrap(
        f'Подписка продлена, {login}!',
        f'<p>Тариф <b>«{tariff_name}»</b> продлён до <b>{end_date[:10]}</b>.</p>'
        f'{_btn(get_site_url()+"/dashboard","Личный кабинет")}',
        unsub_url=uns
    ))

def send_trial_activated(to, login, trial_days, secret_token):
    return _send(to, f'🎁 Пробный период {trial_days} дней — IPTV Billing', _wrap(
        f'Добро пожаловать, {login}!',
        f'<p>Активирован <b>бесплатный пробный период на {trial_days} дней</b>!</p>'
        f'<div style="background:#0f0f1a;border:1px solid #7C3AED;border-radius:8px;padding:16px;'
        f'text-align:center;font-family:monospace;font-size:28px;letter-spacing:8px;'
        f'color:#a78bfa;font-weight:700">{secret_token}</div>'
        f'{_btn(get_site_url()+"/dashboard","Перейти в кабинет")}'
    ))

def send_broadcast(to, subject, body_html, unsub_token):
    uns = f'{get_site_url()}/unsubscribe/{unsub_token}'
    return _send(to, subject, _wrap('', body_html, unsub_url=uns))

def send_test(to: str) -> bool:
    """Send a test email to verify SMTP settings."""
    return _send(to, '✅ Тест SMTP — IPTV Billing', _wrap(
        'SMTP работает!',
        '<p>Это тестовое письмо. Настройки SMTP сохранены и работают корректно.</p>'
    ))
