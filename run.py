"""
Flask application entry point
"""
import logging
import os
from flask import Flask, session, redirect, url_for, render_template, g, request

os.makedirs(os.path.join(os.path.dirname(__file__), 'cache'), exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(os.path.join(os.path.dirname(__file__), 'cache', 'app.log')),
    ]
)
logger = logging.getLogger(__name__)


def create_app():
    app = Flask(__name__)
    app.secret_key = os.environ.get('SECRET_KEY', 'loong-kb-secret-2026')
    app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024

    from app.models import init_db
    init_db()

    from app.routes import auth, qa, admin, wechat_bind
    app.register_blueprint(auth.bp)
    app.register_blueprint(qa.bp)
    app.register_blueprint(admin.bp)
    app.register_blueprint(wechat_bind.bp)

    # RAG-Server Blueprint（local_mode=True 时直调核心函数）
    from app.config import get_rag_server_config
    rag_cfg = get_rag_server_config()
    if rag_cfg.get('local_mode', False):
        from app.rag_server import rag_bp
        app.register_blueprint(rag_bp)
        logger.info("RAG-Server Blueprint 已注册（local_mode=True）")
    else:
        logger.info(f"RAG-Server 独立模式，base_url={rag_cfg.get('base_url','')}")

    @app.before_request
    def load_current_user():
        g.user = None
        g.is_admin = False
        g.has_edit_permission = False
        user_id = session.get('user_id')
        if user_id:
            from app.models import get_user_by_id, get_user_roles as _gur, get_kb_permissions_for_roles, get_db_conn
            g.user = get_user_by_id(user_id)
            g.is_admin = 'admin' in _gur(user_id)
            # Check if user has any KB with can_edit or can_manage
            role_names = _gur(user_id)
            if role_names:
                with get_db_conn() as conn:
                    c = conn.cursor()
                    c.execute('SELECT role_id FROM roles WHERE role_name IN (%s)' %
                              ','.join(['?'] * len(role_names)), role_names)
                    role_ids = [row['role_id'] for row in c.fetchall()]
                perms = get_kb_permissions_for_roles(role_ids)
                g.has_edit_permission = any(
                    p.get('can_edit') or p.get('can_manage')
                    for p in perms.values()
                )

    @app.context_processor
    def inject_globals():
        return dict(
            is_admin=getattr(g, 'is_admin', False),
            has_edit_permission=getattr(g, 'has_edit_permission', False),
            request=request
        )

    @app.template_global()
    def get_user_roles(user_id):
        from app.models import get_user_roles as _gur
        return _gur(user_id)

    @app.route('/')
    def index():
        if not session.get('user_id'):
            return redirect(url_for('auth.login'))
        return redirect(url_for('qa.index'))

    logger.info("Loong KB 应用已启动")

    # 启动内嵌微信 Bot（仅当 token 存在时）
    from app.config import get_wx_bot_config
    wx_cfg = get_wx_bot_config()
    token = wx_cfg.get('ilink_token', '')

    # 数据库 token 优先
    if not token:
        from app.models import get_app_config
        token = get_app_config('wx_bot_token') or ''

    if token:
        from app.wx_bot import start_wx_bot, on_user_bound, on_user_unbound
        import app.wx_bot as _wb

        # 注册用户绑定回调：保存 openid → user_id 到内存（供 wx_bot 收消息时查）
        def _on_bind(openid, user_id):
            logger.info(f"[WxBot] User bound: openid={openid[:20]} -> user_id={user_id}")

        def _on_unbind(openid):
            logger.info(f"[WxBot] User unbound: openid={openid[:20]}")

        on_user_bound(_on_bind)
        on_user_unbound(_on_unbind)

        start_wx_bot(token)
        logger.info("[WxBot] 微信 Bot 已启动")
    else:
        logger.info("[WxBot] ilink_token 为空，请在 管理后台 设置")

    return app


if __name__ == '__main__':
    from app.config import get_server_config
    cfg = get_server_config()
    app = create_app()
    app.run(host=cfg['host'], port=cfg['port'], debug=True, use_reloader=False)
