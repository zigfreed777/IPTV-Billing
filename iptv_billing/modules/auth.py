import secrets, string, re, time, threading
from werkzeug.security import generate_password_hash, check_password_hash
from .db import get_db

_TOKEN_CHARS = (
    ''.join(c for c in string.ascii_uppercase if c != 'I') +
    ''.join(c for c in string.ascii_lowercase if c != 'l') +
    string.digits
)

# ── RATE LIMITER (in-memory, thread-safe) ────────────────────────────────────
_rl_lock = threading.Lock()
_rl_store: dict = {}

def rate_limit(key: str, max_calls: int, window: int) -> bool:
    now = time.monotonic()
    with _rl_lock:
        calls = [t for t in _rl_store.get(key, []) if now - t < window]
        if len(calls) >= max_calls:
            _rl_store[key] = calls
            return False
        calls.append(now)
        _rl_store[key] = calls
        return True

def rate_limit_remaining(key: str, max_calls: int, window: int) -> int:
    now = time.monotonic()
    with _rl_lock:
        calls = [t for t in _rl_store.get(key, []) if now - t < window]
        return max(0, max_calls - len(calls))

# ── CSRF TOKEN ────────────────────────────────────────────────────────────────
from flask import session

def csrf_token() -> str:
    if '_csrf' not in session:
        session['_csrf'] = secrets.token_hex(24)
    return session['_csrf']

def csrf_valid(form_token: str) -> bool:
    expected = session.get('_csrf', '')
    if not expected or not form_token:
        return False
    return secrets.compare_digest(expected, form_token)

# ── PASSWORD (pbkdf2:sha256 + legacy sha256 migration) ───────────────────────
def hash_password(pwd: str) -> str:
    return generate_password_hash(pwd, method='pbkdf2:sha256', salt_length=16)

def verify_password(pwd: str, hashed: str) -> bool:
    if hashed.startswith('pbkdf2:'):
        return check_password_hash(hashed, pwd)
    import hashlib  # legacy migration path
    return hashlib.sha256(pwd.encode()).hexdigest() == hashed

# ── TOKENS ────────────────────────────────────────────────────────────────────
def gen_secret_token() -> str:
    db = get_db()
    while True:
        token = ''.join(secrets.choice(_TOKEN_CHARS) for _ in range(6))
        if not db.execute("SELECT 1 FROM users WHERE secret_token=?", (token,)).fetchone():
            return token

def gen_referral_code() -> str:
    db = get_db()
    while True:
        code = secrets.token_urlsafe(6).upper()[:8]
        if not db.execute("SELECT 1 FROM users WHERE referral_code=?", (code,)).fetchone():
            return code

def gen_token() -> str:
    return secrets.token_urlsafe(32)

# ── VALIDATION ────────────────────────────────────────────────────────────────
def validate_email(email: str) -> bool:
    return bool(re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', email)) and len(email) <= 254

def validate_login(login: str) -> bool:
    return bool(re.match(r'^[a-zA-Z0-9_]{4,32}$', login))

def validate_password(pwd: str) -> bool:
    return len(pwd) >= 6

# ── AUDIT LOG ─────────────────────────────────────────────────────────────────
def log_action(db, user_id, action: str, details: str = None, ip: str = None):
    db.execute(
        "INSERT INTO audit_log (user_id, action, details, ip) VALUES (?,?,?,?)",
        (user_id, action, details, ip)
    )
