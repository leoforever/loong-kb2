"""
Authentication routes
"""
from flask import Blueprint, request, redirect, url_for, render_template, session, flash
import bcrypt
import logging

bp = Blueprint('auth', __name__)
logger = logging.getLogger(__name__)


@bp.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')

        if not username or not password:
            flash('请输入用户名和密码', 'error')
            return render_template('login.html')

        from app.models import get_user_by_username
        user = get_user_by_username(username)

        if not user:
            flash('用户名或密码错误', 'error')
            return render_template('login.html')

        try:
            if not bcrypt.checkpw(password.encode('utf-8'), user['password_hash'].encode('utf-8')):
                flash('用户名或密码错误', 'error')
                return render_template('login.html')
        except Exception:
            flash('登录失败，请重试', 'error')
            return render_template('login.html')

        session['user_id'] = user['user_id']
        session['username'] = user['username']
        logger.info(f"User logged in: {username}")
        return redirect(url_for('qa.index'))

    return render_template('login.html')


@bp.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('auth.login'))


@bp.route('/change-password', methods=['GET', 'POST'])
def change_password():
    """用户修改自己的密码"""
    if not session.get('user_id'):
        return redirect(url_for('auth.login'))

    if request.method == 'POST':
        old_password = request.form.get('old_password', '')
        new_password = request.form.get('new_password', '')
        confirm_password = request.form.get('confirm_password', '')

        if not old_password or not new_password:
            flash('请填写所有字段', 'error')
            return render_template('change_password.html')

        if new_password != confirm_password:
            flash('两次输入的新密码不一致', 'error')
            return render_template('change_password.html')

        if len(new_password) < 6:
            flash('新密码至少6位', 'error')
            return render_template('change_password.html')

        from app.models import get_user_by_id, update_user_password
        user = get_user_by_id(session['user_id'])
        if not user:
            flash('用户不存在', 'error')
            return redirect(url_for('auth.login'))

        # 验证旧密码
        try:
            if not bcrypt.checkpw(old_password.encode('utf-8'), user['password_hash'].encode('utf-8')):
                flash('旧密码错误', 'error')
                return render_template('change_password.html')
        except Exception:
            flash('验证失败，请重试', 'error')
            return render_template('change_password.html')

        # 更新密码
        new_hash = bcrypt.hashpw(new_password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
        update_user_password(user['user_id'], new_hash)
        logger.info(f"User changed password: {user['username']}")
        flash('密码修改成功', 'success')
        return redirect(url_for('qa.index'))

    return render_template('change_password.html')


@bp.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        display_name = request.form.get('display_name', '').strip()

        if not username or not password:
            flash('请输入用户名和密码', 'error')
            return render_template('register.html')

        if len(password) < 6:
            flash('密码至少6位', 'error')
            return render_template('register.html')

        from app.models import get_user_by_username, create_user
        existing = get_user_by_username(username)
        if existing:
            flash('用户名已存在', 'error')
            return render_template('register.html')

        pwd_hash = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
        user_id = create_user(username, pwd_hash, display_name or username)
        session['user_id'] = user_id
        session['username'] = username
        logger.info(f"User registered: {username}")
        return redirect(url_for('qa.index'))

    return render_template('register.html')