import sqlite3, os
from flask import g

DB_PATH = os.path.join(os.path.dirname(__file__), '..', 'billing.db')

def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
        g.db.execute("PRAGMA journal_mode = WAL")
    return g.db

def close_db(e=None):
    db = g.pop('db', None)
    if db is not None:
        db.close()

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript("""
-- ── SERVICES (Услуги — группы пакетов) ──────────────────────────────────
CREATE TABLE IF NOT EXISTS services (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    description TEXT,
    icon        TEXT DEFAULT '📦',
    sort_order  INTEGER DEFAULT 0,
    is_active   INTEGER DEFAULT 1,
    created_at  TEXT DEFAULT (datetime('now'))
);
-- ── PACKAGES (Пакеты — то что подключает пользователь) ──────────────────
CREATE TABLE IF NOT EXISTS packages (
    id              INTEGER PRIMARY KEY,
    service_id      INTEGER NOT NULL REFERENCES services(id),
    name            TEXT NOT NULL,
    description     TEXT,
    price_per_day   REAL NOT NULL,
    connections     INTEGER NOT NULL DEFAULT 1,
    allow_extra_connections  INTEGER DEFAULT 0,
    extra_connection_price   REAL DEFAULT 0.0,
    max_extra_connections    INTEGER DEFAULT 5,
    is_active       INTEGER DEFAULT 1,
    sort_order      INTEGER DEFAULT 0,
    created_at      TEXT DEFAULT (datetime('now'))
);
-- ── USERS ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS users (
    id               INTEGER PRIMARY KEY,
    login            TEXT UNIQUE NOT NULL,
    password_hash    TEXT NOT NULL,
    email            TEXT UNIQUE NOT NULL,
    email_confirmed  INTEGER DEFAULT 0,
    confirm_token    TEXT,
    confirm_sent_at  TEXT,
    reset_token      TEXT,
    reset_token_exp  TEXT,
    secret_token     TEXT UNIQUE NOT NULL,
    balance          REAL DEFAULT 0.0,
    referral_code    TEXT UNIQUE,
    referred_by      INTEGER REFERENCES users(id),
    comment          TEXT,
    is_active        INTEGER DEFAULT 1,
    is_blocked       INTEGER DEFAULT 0,
    block_reason     TEXT,
    last_login       TEXT,
    last_ip          TEXT,
    trial_used       INTEGER DEFAULT 0,
    email_news       INTEGER DEFAULT 1,
    unsub_token      TEXT,
    phone            TEXT,
    birthdate        TEXT,
    avatar_url       TEXT,
    timezone         TEXT DEFAULT 'UTC',
    language         TEXT DEFAULT 'ru',
    stream_format    TEXT DEFAULT 'ts',
    preferred_server TEXT,
    block_adult      INTEGER DEFAULT 0,
    auto_renew       INTEGER DEFAULT 0,
    registered_at    TEXT DEFAULT (datetime('now')),
    updated_at       TEXT DEFAULT (datetime('now'))
);
-- ── USER SUBSCRIPTIONS (активные пакеты пользователя) ───────────────────
CREATE TABLE IF NOT EXISTS user_packages (
    id          INTEGER PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id),
    package_id  INTEGER NOT NULL REFERENCES packages(id),
    extra_connections INTEGER NOT NULL DEFAULT 1,
    started_at  TEXT DEFAULT (datetime('now')),
    active      INTEGER DEFAULT 1,
    stopped_at  TEXT
);
-- ── DAILY CHARGES (история посуточных списаний) ──────────────────────────
CREATE TABLE IF NOT EXISTS charges (
    id             INTEGER PRIMARY KEY,
    user_id        INTEGER NOT NULL REFERENCES users(id),
    package_id     INTEGER NOT NULL REFERENCES packages(id),
    amount         REAL NOT NULL,
    charged_at     TEXT DEFAULT (datetime('now'))
);
-- ── TOPUPS ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS topups (
    id           INTEGER PRIMARY KEY,
    user_id      INTEGER NOT NULL REFERENCES users(id),
    amount       REAL NOT NULL,
    method       TEXT NOT NULL,
    status       TEXT DEFAULT 'pending',
    external_id  TEXT,
    note         TEXT,
    created_at   TEXT DEFAULT (datetime('now')),
    confirmed_at TEXT
);
-- ── ACTIVE STREAMS (одновременные подключения для API) ───────────────────
CREATE TABLE IF NOT EXISTS active_streams (
    id          INTEGER PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id),
    stream_id   TEXT NOT NULL,
    extra_connections INTEGER NOT NULL DEFAULT 1,
    started_at  TEXT DEFAULT (datetime('now')),
    last_ping   TEXT DEFAULT (datetime('now')),
    client_ip   TEXT,
    UNIQUE(user_id, stream_id)
);
-- ── REFERRALS ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS referrals (
    id           INTEGER PRIMARY KEY,
    referrer_id  INTEGER NOT NULL REFERENCES users(id),
    referred_id  INTEGER NOT NULL REFERENCES users(id),
    bonus_amount REAL DEFAULT 0.0,
    paid         INTEGER DEFAULT 0,
    created_at   TEXT DEFAULT (datetime('now'))
);
-- ── AUDIT LOG ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS audit_log (
    id         INTEGER PRIMARY KEY,
    user_id    INTEGER REFERENCES users(id),
    action     TEXT NOT NULL,
    details    TEXT,
    ip         TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
-- ── PROMO CODES ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS promocodes (
    id          INTEGER PRIMARY KEY,
    code        TEXT UNIQUE NOT NULL,
    type        TEXT NOT NULL CHECK(type IN ('percent','fixed','days','trial')),
    value       REAL NOT NULL,
    max_uses    INTEGER DEFAULT 1,
    uses        INTEGER DEFAULT 0,
    package_id  INTEGER REFERENCES packages(id),
    valid_from  TEXT,
    valid_until TEXT,
    is_active   INTEGER DEFAULT 1,
    comment     TEXT,
    created_at  TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS promocode_uses (
    id              INTEGER PRIMARY KEY,
    promocode_id    INTEGER NOT NULL REFERENCES promocodes(id),
    user_id         INTEGER NOT NULL REFERENCES users(id),
    discount_amount REAL DEFAULT 0,
    used_at         TEXT DEFAULT (datetime('now')),
    UNIQUE(promocode_id, user_id)
);
-- ── NOTIFICATIONS ───────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS notification_log (
    id          INTEGER PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id),
    type        TEXT NOT NULL,
    days_before INTEGER,
    sent_at     TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS notify_settings (
    id            INTEGER PRIMARY KEY DEFAULT 1,
    enabled       INTEGER DEFAULT 1,
    days_before   TEXT DEFAULT '7,3,1',
    trial_days    INTEGER DEFAULT 3,
    trial_enabled INTEGER DEFAULT 1,
    updated_at    TEXT DEFAULT (datetime('now'))
);
-- ── SMTP ────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS smtp_settings (
    id        INTEGER PRIMARY KEY DEFAULT 1,
    host      TEXT DEFAULT 'smtp.yandex.ru',
    port      INTEGER DEFAULT 465,
    user      TEXT DEFAULT '',
    password  TEXT DEFAULT '',
    from_name TEXT DEFAULT 'IPTV Billing',
    site_url  TEXT DEFAULT 'http://localhost:5003',
    enabled   INTEGER DEFAULT 1,
    updated_at TEXT DEFAULT (datetime('now'))
);
-- ── BROADCASTS ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS broadcasts (
    id           INTEGER PRIMARY KEY,
    subject      TEXT NOT NULL,
    body_html    TEXT NOT NULL,
    filter_type  TEXT DEFAULT 'all',
    filter_value TEXT,
    status       TEXT DEFAULT 'draft',
    sent_count   INTEGER DEFAULT 0,
    created_by   INTEGER REFERENCES users(id),
    created_at   TEXT DEFAULT (datetime('now')),
    sent_at      TEXT
);
-- ── CHAT ────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS chat_messages (
    id          INTEGER PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id),
    from_admin  INTEGER DEFAULT 0,
    body        TEXT NOT NULL,
    is_read     INTEGER DEFAULT 0,
    created_at  TEXT DEFAULT (datetime('now'))
);
-- ── INDEXES ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS site_settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    label      TEXT,
    group_     TEXT DEFAULT 'general',
    type       TEXT DEFAULT 'text',
    updated_at TEXT DEFAULT (datetime('now'))
);
INSERT OR IGNORE INTO site_settings (key,value,label,group_,type) VALUES
    ('brand_name','IPTV Billing','Название сервиса','brand','text'),
    ('brand_tagline','Смотри больше, плати меньше','Слоган','brand','text'),
    ('brand_logo_emoji','📺','Логотип эмодзи','brand','text'),
    ('brand_color','#7C3AED','Основной цвет','brand','color'),
    ('brand_color2','#3B82F6','Второй цвет','brand','color'),
    ('support_email','support@example.com','Email поддержки','brand','text'),
    ('support_telegram','','Telegram','brand','text'),
    ('trial_enabled','1','Пробный период','billing','bool'),
    ('trial_days','3','Дней пробного периода','billing','int'),
    ('trial_package_id','1','ID пакета для пробного','billing','int'),
    ('referral_bonus','0','Бонус за реферала (₽)','billing','float'),
    ('min_topup','10','Минимальное пополнение (₽)','billing','int'),
    ('max_topup','50000','Максимальное пополнение (₽)','billing','int'),
    ('charge_hour','0','Час списания UTC (0-23)','billing','int'),
    ('low_balance_days','3','Уведомить при балансе < N дней','billing','int'),
    ('grace_period_hours','24','Грейс-период (ч)','billing','int'),
    ('cab_show_referrals','1','Реферальный блок','cabinet','bool'),
    ('cab_show_chat','1','Чат поддержки','cabinet','bool'),
    ('cab_show_media','1','Раздел Медиа','cabinet','bool'),
    ('cab_welcome_text','Добро пожаловать','Приветственный текст','cabinet','text'),
    ('cab_support_text','Напишите нам — обычно отвечаем в течение нескольких часов','Текст поддержки','cabinet','text'),
    ('cab_topup_methods','card,crypto,transfer','Способы пополнения','cabinet','text'),
    ('playlist_ttl','30','TTL потока (сек)','cabinet','int'),
    ('stream_formats','ts,m3u8,hls','Форматы потоков','cabinet','text'),
    ('reg_enabled','1','Регистрация открыта','security','bool'),
    ('reg_invite_only','0','Только по приглашению','security','bool'),
    ('login_attempts','10','Попыток входа','security','int'),
    ('login_window_sec','300','Окно блокировки (сек)','security','int'),
    ('session_days','30','Длительность сессии (дней)','security','int'),
    ('require_email_confirm','1','Подтверждение email','security','bool'),
    ('notif_enabled','1','Уведомления об истечении','notify','bool'),
    ('notif_days_before','7,3,1','За сколько дней уведомлять','notify','text');

CREATE INDEX IF NOT EXISTS idx_users_email       ON users(email);
CREATE INDEX IF NOT EXISTS idx_users_login       ON users(login);
CREATE INDEX IF NOT EXISTS idx_users_secret      ON users(secret_token);
CREATE TABLE IF NOT EXISTS servers (
    id          INTEGER PRIMARY KEY,
    package_id  INTEGER NOT NULL REFERENCES packages(id),
    name        TEXT NOT NULL,
    ip          TEXT NOT NULL,
    proxy_port  INTEGER NOT NULL DEFAULT 8080,
    api_port    INTEGER NOT NULL DEFAULT 4444,
    comment     TEXT,
    api_token   TEXT DEFAULT '',
    is_active   INTEGER DEFAULT 1,
    sort_order  INTEGER DEFAULT 0,
    created_at  TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS user_server_prefs (
    id          INTEGER PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id),
    package_id  INTEGER NOT NULL REFERENCES packages(id),
    server_id   INTEGER NOT NULL REFERENCES servers(id),
    updated_at  TEXT DEFAULT (datetime('now')),
    UNIQUE(user_id, package_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_up_active_unique ON user_packages(user_id, package_id) WHERE active=1;
CREATE INDEX IF NOT EXISTS idx_up_user           ON user_packages(user_id);
CREATE INDEX IF NOT EXISTS idx_up_active         ON user_packages(user_id, active);
CREATE INDEX IF NOT EXISTS idx_charges_user      ON charges(user_id);
CREATE INDEX IF NOT EXISTS idx_streams_user      ON active_streams(user_id);
CREATE INDEX IF NOT EXISTS idx_chat_user         ON chat_messages(user_id);
CREATE INDEX IF NOT EXISTS idx_audit_user        ON audit_log(user_id);
CREATE INDEX IF NOT EXISTS idx_promo_code        ON promocodes(code);
-- ── SEED DATA ────────────────────────────────────────────────────────────
INSERT OR IGNORE INTO notify_settings (id) VALUES (1);
INSERT OR IGNORE INTO smtp_settings (id) VALUES (1);
INSERT OR IGNORE INTO services (id, name, description, icon, sort_order) VALUES
    (1, 'Телеканалы',  'Прямой эфир телевизионных каналов', '📺', 1),
    (2, 'Медиатека',   'Фильмы, сериалы, передачи по запросу', '🎬', 2),
    (3, 'Радио',       'Радиостанции онлайн', '📻', 3),
    (4, 'Веб-камеры',  'Трансляции с веб-камер', '📷', 4);
INSERT OR IGNORE INTO packages (id, service_id, name, description, price_per_day, connections, sort_order) VALUES
    (1, 1, 'Базовый',   '200+ каналов · SD',          6.63, 1, 1),
    (2, 1, 'Стандарт',  '400+ каналов · HD',          11.63, 2, 2),
    (3, 1, 'Премиум',   '600+ каналов · Full HD',     19.97, 4, 3),
    (4, 1, 'Ультра',    '800+ каналов · 4K + архив',  33.30, 6, 4),
    (5, 2, 'Медиатека', '10 000+ фильмов и сериалов',  9.97, 2, 1),
    (6, 3, 'Радио',     '500+ радиостанций',            3.30, 1, 1),
    (7, 4, 'Камеры',    '1000+ веб-камер мира',         3.30, 1, 1);
""")
    conn.commit()
    conn.close()
