import os
from flask import Flask, redirect, url_for
from .modules.db import init_db, close_db
from .modules.routes_auth import bp as auth_bp
from .modules.routes_dashboard import bp as dashboard_bp
from .modules.routes_admin import bp as admin_bp
from .modules.routes_api import bp as api_bp

def create_app():
    app = Flask(__name__, template_folder='templates', static_folder='static')
    secret = os.getenv('SECRET_KEY')
    if not secret:
        import warnings; warnings.warn("SECRET_KEY not set!")
        secret = os.urandom(32)
    app.secret_key = secret
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Lax',
        SESSION_COOKIE_SECURE=os.getenv('FLASK_ENV') == 'production',
        PERMANENT_SESSION_LIFETIME=86400 * 30,
    )
    init_db()
    app.teardown_appcontext(close_db)
    app.register_blueprint(auth_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(api_bp)
    from .modules import scheduler
    from .modules.context_processors import inject_admin_context, inject_user_context, inject_site_settings
    app.context_processor(inject_admin_context)
    app.context_processor(inject_user_context)
    app.context_processor(inject_site_settings)
    scheduler.start()
    @app.route('/')
    def index(): return redirect(url_for('landing'))
    @app.route('/home')
    def landing():
        from flask import render_template
        return render_template('landing.html')
    @app.errorhandler(404)
    def e404(e): return redirect(url_for('dashboard.index'))
    return app
