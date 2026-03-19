from flask import session, request
from .db import get_db

def inject_admin_context():
    ctx = {}
    if session.get('user_id'):
        try:
            db = get_db()
            ctx['pending_topups']   = db.execute("SELECT COUNT(*) FROM topups WHERE status='pending'").fetchone()[0]
            ctx['admin_unread_chat'] = db.execute("SELECT COUNT(*) FROM chat_messages WHERE from_admin=0 AND is_read=0").fetchone()[0]
        except Exception:
            ctx['pending_topups'] = 0
            ctx['admin_unread_chat'] = 0
    return ctx

def inject_user_context():
    ctx = {'unread_chat': 0}
    if session.get('user_id') and request.path.startswith('/dashboard'):
        try:
            db = get_db()
            ctx['unread_chat'] = db.execute(
                "SELECT COUNT(*) FROM chat_messages WHERE user_id=? AND from_admin=1 AND is_read=0",
                (session['user_id'],)).fetchone()[0]
        except Exception:
            pass
    return ctx

def inject_site_settings():
    """Makes S (settings) available in every template."""
    try:
        from .settings import _load
        return {'S': _load()}
    except Exception:
        return {'S': {}}
