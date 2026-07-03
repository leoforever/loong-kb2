"""
Admin routes - manage users, roles, KB configs, permissions
"""
from flask import Blueprint, request, render_template, redirect, url_for, jsonify, session, flash
import bcrypt
import requests
import logging

bp = Blueprint('admin', __name__)
logger = logging.getLogger(__name__)


def require_admin(user_id):
    """Check if user has admin role"""
    from app.models import get_user_roles as _gur
    return 'admin' in _gur(user_id)


def admin_required(f):
    """Decorator to require admin role"""
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('user_id') or not require_admin(session['user_id']):
            return '需要管理员权限', 403
        return f(*args, **kwargs)
    return decorated


def _get_user_role_ids(user_id):
    """Get role_ids for a user by name"""
    from app.models import get_user_roles, get_db_conn
    role_names = get_user_roles(user_id)
    if not role_names:
        return []
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute('SELECT role_id FROM roles WHERE role_name IN (%s)' %
                  ','.join(['?'] * len(role_names)), role_names)
        return [row['role_id'] for row in c.fetchall()]


def require_kb_manage(kb_id):
    """Check if current user has can_manage permission on a KB"""
    from app.models import get_kb_permissions_for_roles
    user_id = session.get('user_id')
    if not user_id:
        return False
    role_ids = _get_user_role_ids(user_id)
    perms = get_kb_permissions_for_roles(role_ids)
    perm = perms.get(kb_id, {})
    return bool(perm.get('can_manage'))


def require_kb_edit(kb_id):
    """Check if current user has can_edit or can_manage permission on a KB"""
    from app.models import get_kb_permissions_for_roles
    user_id = session.get('user_id')
    if not user_id:
        return False
    role_ids = _get_user_role_ids(user_id)
    perms = get_kb_permissions_for_roles(role_ids)
    perm = perms.get(kb_id, {})
    return bool(perm.get('can_edit') or perm.get('can_manage'))


def require_kb_manage_or_admin(kb_id):
    """Allow if admin role OR has KB manage permission"""
    if session.get('user_id') and require_admin(session['user_id']):
        return True
    return require_kb_manage(kb_id)


def require_kb_edit_or_admin(kb_id):
    """Allow if admin role OR has KB edit/manage permission"""
    if session.get('user_id') and require_admin(session['user_id']):
        return True
    return require_kb_edit(kb_id)


def kb_manage_required(f):
    """Decorator to require KB manage permission (or admin role bypasses this)"""
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        kb_id = kwargs.get('kb_id')
        if not session.get('user_id'):
            return '请先登录', 403
        if not require_kb_manage_or_admin(kb_id):
            return '无权限管理该知识库', 403
        return f(*args, **kwargs)
    return decorated


def kb_edit_required(f):
    """Decorator to require KB edit/manage permission (or admin role bypasses this)"""
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        kb_id = kwargs.get('kb_id')
        if not session.get('user_id'):
            return '请先登录', 403
        if not require_kb_edit_or_admin(kb_id):
            return '无权限操作该知识库', 403
        return f(*args, **kwargs)
    return decorated


# ==================== User Management ====================

@bp.route('/admin/users')
@admin_required
def users():
    from app.models import get_all_users, get_all_roles
    users = get_all_users()
    roles = get_all_roles()
    return render_template('admin_users.html', users=users, roles=roles)


@bp.route('/admin/users', methods=['POST'])
@admin_required
def create_user():
    username = request.form.get('username', '').strip()
    password = request.form.get('password', '')
    display_name = request.form.get('display_name', '').strip()
    role_ids = request.form.getlist('role_ids')

    if not username or not password:
        flash('用户名和密码不能为空', 'error')
        return redirect(url_for('admin.users'))

    from app.models import get_user_by_username, create_user as mk_user
    if get_user_by_username(username):
        flash('用户名已存在', 'error')
        return redirect(url_for('admin.users'))

    pwd_hash = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
    user_id = mk_user(username, pwd_hash, display_name or username)

    from app.models import get_db_conn
    with get_db_conn() as conn:
        c = conn.cursor()
        for rname in role_ids:
            c.execute('SELECT role_id FROM roles WHERE role_name = ?', (rname,))
            row = c.fetchone()
            if row:
                c.execute('INSERT OR IGNORE INTO user_roles (user_id, role_id) VALUES (?, ?)',
                          (user_id, row['role_id']))

    flash(f'用户 {username} 创建成功', 'success')
    return redirect(url_for('admin.users'))


@bp.route('/admin/users/<username>/roles', methods=['POST'])
@admin_required
def update_user_roles(username):
    role_names = request.form.getlist('role_ids')
    from app.models import get_db_conn, get_user_by_username

    user = get_user_by_username(username)
    if not user:
        flash('用户不存在', 'error')
        return redirect(url_for('admin.users'))

    # Update via user_roles mapping table
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute('DELETE FROM user_roles WHERE user_id = ?', (user['user_id'],))
        for rname in role_names:
            c.execute('SELECT role_id FROM roles WHERE role_name = ?', (rname,))
            row = c.fetchone()
            if row:
                c.execute('INSERT INTO user_roles (user_id, role_id) VALUES (?, ?)',
                          (user['user_id'], row['role_id']))

    flash('角色分配已更新', 'success')
    return redirect(url_for('admin.users'))


@bp.route('/admin/users/<int:user_id>/delete', methods=['POST'])
@admin_required
def delete_user(user_id):
    from app.models import get_db_conn
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute('DELETE FROM user_roles WHERE user_id = ?', (user_id,))
        c.execute('DELETE FROM query_log WHERE user_id = ?', (user_id,))
        c.execute('DELETE FROM users WHERE user_id = ?', (user_id,))
    flash('用户已删除', 'success')
    return redirect(url_for('admin.users'))


# ==================== Role Management ====================

@bp.route('/admin/roles')
@admin_required
def roles():
    from app.models import get_all_roles, get_all_role_kb_permissions, get_all_kbs, get_all_users
    roles = get_all_roles()
    perms = get_all_role_kb_permissions()
    kbs = get_all_kbs()
    # Build user count per role_id
    users = get_all_users()
    role_user_count = {}
    for u in users:
        roles_str = u['roles'] if 'roles' in u.keys() else None
        if roles_str:
            for r in roles_str.split(','):
                r = r.strip()
                if r:
                    from app.models import get_role_by_name
                    role = get_role_by_name(r)
                    if role:
                        role_user_count[role['role_id']] = role_user_count.get(role['role_id'], 0) + 1
    return render_template('admin_roles.html', roles=roles, kbs=kbs, perms=perms, role_user_count=role_user_count)


@bp.route('/admin/roles', methods=['POST'])
@admin_required
def create_role():
    role_name = request.form.get('role_name', '').strip()
    description = request.form.get('description', '').strip()
    if not role_name:
        flash('角色名不能为空', 'error')
        return redirect(url_for('admin.roles'))
    from app.models import get_db_conn
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute('INSERT OR IGNORE INTO roles (role_name, description) VALUES (?, ?)',
                  (role_name, description))
    flash(f'角色 {role_name} 创建成功', 'success')
    return redirect(url_for('admin.roles'))


@bp.route('/admin/roles/<int:role_id>/delete', methods=['POST'])
@admin_required
def delete_role(role_id):
    from app.models import get_db_conn
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute('DELETE FROM user_roles WHERE role_id = ?', (role_id,))
        c.execute('DELETE FROM role_kb_permissions WHERE role_id = ?', (role_id,))
        c.execute('DELETE FROM roles WHERE role_id = ?', (role_id,))
    flash('角色已删除', 'success')
    return redirect(url_for('admin.roles'))


@bp.route('/admin/roles/<int:role_id>/permissions', methods=['GET'])
@admin_required
def get_role_permissions(role_id):
    from app.models import get_role_kb_permissions
    perms = get_role_kb_permissions(role_id)
    return jsonify([{'kb_id': p['kb_id'], 'can_access': p['can_access'], 'can_edit': p['can_edit'], 'can_manage': p['can_manage']} for p in perms])


# ==================== KB Management ====================

@bp.route('/admin/kbs')
def kbs():
    from app.models import get_all_kbs, get_all_roles, get_all_role_kb_permissions, get_kb_permissions_for_roles, get_user_roles, get_db_conn
    from app.config import get_embedding_config
    # Permission check: must be admin OR have at least one KB with can_edit/can_manage
    user_id = session.get('user_id')
    if not user_id:
        return redirect(url_for('auth.login'))
    role_names = get_user_roles(user_id)
    is_admin = 'admin' in role_names
    editable_kb_ids = set()
    accessible_kb_ids = set()
    manageable_kb_ids = set()
    if is_admin:
        all_kbs = get_all_kbs()
        accessible_kb_ids = {kb['kb_id'] for kb in all_kbs}
        manageable_kb_ids = {kb['kb_id'] for kb in all_kbs}
    elif role_names:
        with get_db_conn() as conn:
            c = conn.cursor()
            c.execute('SELECT role_id FROM roles WHERE role_name IN (%s)' %
                      ','.join(['?'] * len(role_names)), role_names)
            role_ids = [row['role_id'] for row in c.fetchall()]
        user_perms = get_kb_permissions_for_roles(role_ids)
        editable_kb_ids = {
            kb_id for kb_id, p in user_perms.items()
            if p.get('can_edit') or p.get('can_manage')
        }
        manageable_kb_ids = {
            kb_id for kb_id, p in user_perms.items()
            if p.get('can_manage')
        }
        accessible_kb_ids = {
            kb_id for kb_id, p in user_perms.items()
            if p.get('can_access')
        }
        if not accessible_kb_ids:
            return '无权限访问', 403

    kbs = get_all_kbs()
    roles = get_all_roles()
    perms = get_all_role_kb_permissions()
    edit_kb_id = request.args.get('edit', type=int)

    # 每个 KB 的 embedding/reranking 模型信息（从 RAG-Server config 读取）
    emb_cfg = get_embedding_config()
    local_emb_model = None
    if emb_cfg.get('provider') == 'siliconflow':
        local_emb_model = emb_cfg.get('siliconflow', {}).get('model')
    elif emb_cfg.get('provider') == 'ollama':
        local_emb_model = emb_cfg.get('ollama', {}).get('model')
    elif emb_cfg.get('provider') == 'openai':
        local_emb_model = emb_cfg.get('openai', {}).get('model')

    for kb in kbs:
        if kb.get('template_type') == 'qa':
            kb['_embedding_model'] = local_emb_model
            kb['_reranking_model'] = None
        elif kb.get('rag_dataset_id'):
            # RAG KB: embedding/reranking 由 RAG-Server 统一管理
            kb['_embedding_model'] = local_emb_model
            kb['_reranking_model'] = emb_cfg.get('reranker', {}).get('siliconflow', {}).get('model', 'BAAI/bge-reranker-v2-m3')

    return render_template('admin_kbs.html', kbs=kbs, roles=roles, perms=perms,
                            edit_kb_id=edit_kb_id,
                            is_admin=is_admin,
                            editable_kb_ids=editable_kb_ids,
                            accessible_kb_ids=accessible_kb_ids,
                            manageable_kb_ids=manageable_kb_ids,
                            embedding_config=emb_cfg)


def _user_has_any_kb_manage():
    """Check if current user has can_manage on any KB (or is admin)"""
    if session.get('user_id') and require_admin(session['user_id']):
        return True
    from app.models import get_kb_permissions_for_roles, get_user_roles
    user_id = session['user_id']
    role_names = get_user_roles(user_id)
    if not role_names:
        return False
    role_ids = []
    from app.models import get_db_conn
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute('SELECT role_id FROM roles WHERE role_name IN (%s)' %
                  ','.join(['?'] * len(role_names)), role_names)
        role_ids = [row['role_id'] for row in c.fetchall()]
    perms = get_kb_permissions_for_roles(role_ids)
    return any(p.get('can_manage') for p in perms.values())


@bp.route('/admin/kbs', methods=['POST'])
@admin_required
def create_kb():
    """手动注册已有 RAG KB（通过 Rag dataset ID）"""
    kb_name = request.form.get('kb_name', '').strip()
    description = request.form.get('description', '').strip()
    rag_dataset_id = request.form.get('rag_dataset_id', '').strip()

    if not kb_name:
        flash('知识库名称不能为空', 'error')
        return redirect(url_for('admin.kbs'))

    from app.models import create_kb
    kb_id = create_kb(kb_name, description, template_type=None, rag_dataset_id=rag_dataset_id or None)
    flash(f'知识库 {kb_name} 创建成功', 'success')
    return redirect(url_for('admin.kbs'))


@bp.route('/admin/kbs/<int:kb_id>', methods=['GET', 'POST'])
@kb_manage_required
def edit_kb(kb_id):
    from app.models import get_kb_by_id, update_kb
    kb = get_kb_by_id(kb_id)
    if not kb:
        return '知识库不存在', 404

    if request.method == 'POST':
        update_kb(
            kb_id,
            request.form.get('kb_name', '').strip(),
            request.form.get('description', '').strip(),
            request.form.get('template_type', '').strip() or None,
        )
        flash('知识库已更新', 'success')
        return redirect(url_for('admin.kbs'))

    return redirect(url_for('admin.kbs', edit=kb_id))


@bp.route('/admin/kbs/<int:kb_id>/delete', methods=['POST'])
@kb_manage_required
def delete_kb(kb_id):
    """删除知识库：删除本地记录，RAG KB 同时删除 RAG-Server 端数据"""
    from app.models import get_kb_by_id, delete_kb as db_delete_kb
    kb = get_kb_by_id(kb_id)
    if not kb:
        flash('知识库不存在', 'error')
        return redirect(url_for('admin.kbs'))

    kb = dict(kb) if hasattr(kb, 'keys') else kb
    rag_id = kb.get('rag_dataset_id')

    # 删除本地记录
    db_delete_kb(kb_id)

    # RAG KB：同时删除 RAG-Server 端数据
    if rag_id:
        from app.services.rag_kb_service import delete_dataset
        delete_dataset(rag_id)

    flash('知识库已删除', 'success')
    return redirect(url_for('admin.kbs'))


@bp.route('/admin/kbs/from-template', methods=['POST'])
@admin_required
def create_kb_from_template_alias():
    """别名路由：前端表单提交到 /admin/kbs/from-template"""
    import logging
    logger = logging.getLogger(__name__)
    kb_name = request.form.get('kb_name', '').strip()
    description = request.form.get('description', '').strip()
    template_id = request.form.get('template_id', '').strip()
    logger.warning(f"[DEBUG from-template] kb_name={kb_name!r} template_id={template_id!r}")

    # 直接内联 create_kb_from_template 逻辑，不跨函数调用
    if not kb_name:
        flash('知识库名称不能为空', 'error')
        return redirect(url_for('admin.kbs'))

    # 问答知识库
    if template_id == 'qa':
        from app.models import create_local_qa_kb, get_role_by_name, set_kb_role_permission
        kb_id = create_local_qa_kb(kb_name, description)
        admin_role = get_role_by_name('admin')
        if admin_role:
            set_kb_role_permission(admin_role['role_id'], kb_id, 1, 1, 1)
        flash(f'问答知识库「{kb_name}」创建成功', 'success')
        return redirect(url_for('admin.kbs'))

    # RAG-Server 文档知识库
    TEMPLATE_MODE_MAP = {
        'text_plain': 'general',
        'hierarchical_full': 'parent_child',
        'hierarchical_paragraph': 'paragraph',
    }
    if template_id not in TEMPLATE_MODE_MAP:
        flash(f'未知模板: {template_id}', 'error')
        return redirect(url_for('admin.kbs'))

    from app.services.rag_kb_service import create_dataset
    ds_result = create_dataset(kb_name, description)
    if 'error' in ds_result:
        logger.error(f"[DEBUG from-template] create_dataset error: {ds_result}")
        flash(f'创建知识库失败：{ds_result["error"]}', 'error')
        return redirect(url_for('admin.kbs'))

    rag_dataset_id = ds_result['id']
    from app.models import create_kb, get_role_by_name, set_kb_role_permission
    kb_id = create_kb(kb_name, description, template_type=template_id, rag_dataset_id=rag_dataset_id)
    admin_role = get_role_by_name('admin')
    if admin_role:
        set_kb_role_permission(admin_role['role_id'], kb_id, 1, 1, 1)
    flash(f'知识库「{kb_name}」创建成功', 'success')
    return redirect(url_for('admin.kbs'))


@bp.route('/admin/kbs/create', methods=['POST'])
@admin_required
def create_kb_from_template():
    """从模板创建知识库（RAG-Server 模式）
    
    POST body:
      kb_name, description, template_id
      - template_id='qa': 本地问答知识库（不变）
      - template_id='text_plain'|'hierarchical_full'|'hierarchical_paragraph': RAG-Server 文档知识库
    """
    kb_name = request.form.get('kb_name', '').strip()
    description = request.form.get('description', '').strip()
    template_id = request.form.get('template_id', '').strip()

    if not kb_name:
        flash('知识库名称不能为空', 'error')
        return redirect(url_for('admin.kbs'))

    # ===== 问答知识库（本地 FAISS） =====
    if template_id == 'qa':
        from app.models import create_local_qa_kb, get_role_by_name, set_kb_role_permission
        kb_id = create_local_qa_kb(kb_name, description)
        admin_role = get_role_by_name('admin')
        if admin_role:
            set_kb_role_permission(admin_role['role_id'], kb_id, 1, 1, 1)
        flash(f'问答知识库「{kb_name}」创建成功', 'success')
        return redirect(url_for('admin.kbs'))

    # ===== RAG-Server 文档知识库 =====
    # 模板 → RAG-Server mode 映射
    TEMPLATE_MODE_MAP = {
        'text_plain': 'general',
        'hierarchical_full': 'parent_child',
        'hierarchical_paragraph': 'paragraph',
    }
    if template_id not in TEMPLATE_MODE_MAP:
        flash(f'未知模板: {template_id}', 'error')
        return redirect(url_for('admin.kbs'))

    mode = TEMPLATE_MODE_MAP[template_id]

    # 1. 在 RAG-Server 创建空 KB
    from app.services.rag_kb_service import create_dataset
    ds_result = create_dataset(kb_name, description)
    if 'error' in ds_result:
        flash(f'创建知识库失败：{ds_result["error"]}', 'error')
        return redirect(url_for('admin.kbs'))

    rag_dataset_id = ds_result['id']
    logger.info(f"[CreateKB] RAG KB created: {rag_dataset_id}")

    # 2. 写入本地记录
    from app.models import create_kb
    kb_id = create_kb(kb_name, description, template_type=template_id, rag_dataset_id=rag_dataset_id)

    # 3. 给 admin 加权限
    from app.models import get_role_by_name, set_kb_role_permission
    admin_role = get_role_by_name('admin')
    if admin_role:
        set_kb_role_permission(admin_role['role_id'], kb_id, 1, 1, 1)

    flash(f'知识库「{kb_name}」创建成功（RAG-Server）', 'success')
    return redirect(url_for('admin.kbs'))


@bp.route('/admin/kbs/<int:kb_id>/documents/<path:doc_id>', methods=['DELETE'])
@kb_edit_required
def delete_kb_document(kb_id, doc_id):
    """删除知识库中的文档"""
    from app.models import get_kb_by_id
    kb = get_kb_by_id(kb_id)
    if not kb:
        return jsonify({'error': '知识库不存在'}), 404
    kb = dict(kb) if hasattr(kb, 'keys') else kb

    # RAG-Server KB
    if kb.get('rag_dataset_id'):
        from app.services.rag_kb_service import RAGServerKBService
        svc = RAGServerKBService(rag_dataset_id=kb['rag_dataset_id'], kb_name=kb.get('kb_name', ''))
        result = svc.delete_document(doc_id)
        if 'error' in result:
            return jsonify(result), 500
        return jsonify({'success': True})

    return jsonify({'error': '知识库不存在'}), 404


@bp.route('/admin/kbs/<int:kb_id>/upload', methods=['POST'])
@kb_edit_required
def upload_kb_document(kb_id):
    """上传文件到知识库（使用默认配置）"""
    from app.models import get_kb_by_id
    kb = get_kb_by_id(kb_id)
    if not kb:
        return jsonify({'error': '知识库不存在'}), 404
    kb = dict(kb) if hasattr(kb, 'keys') else kb

    if 'file' not in request.files:
        return jsonify({'error': '未找到上传文件'}), 400

    file = request.files['file']
    if not file.filename:
        return jsonify({'error': '文件名为空'}), 400

    import os, uuid
    tmp_dir = '/tmp/loong-kb2-uploads'
    os.makedirs(tmp_dir, exist_ok=True)
    suffix = os.path.splitext(file.filename)[1]
    tmp_path = os.path.join(tmp_dir, f'{uuid.uuid4().hex}{suffix}')
    file.save(tmp_path)

    try:
        if kb.get('rag_dataset_id'):
            from app.services.rag_kb_service import RAGServerKBService
            svc = RAGServerKBService(rag_dataset_id=kb['rag_dataset_id'], kb_name=kb.get('kb_name', ''))
            result = svc.upload_document(tmp_path, filename=file.filename)
        else:
            return jsonify({'error': '非 RAG 知识库或知识库不存在'}), 400
    finally:
        try:
            os.remove(tmp_path)
        except Exception:
            pass

    if 'error' in result:
        return jsonify(result), 500
    return jsonify(result)


@bp.route('/admin/kbs/<int:kb_id>/documents', methods=['GET'])
def get_kb_documents(kb_id):
    """获取知识库的文档列表（需要 can_access 权限）"""
    from app.models import get_kb_by_id, get_kb_permissions_for_roles, get_user_roles, get_db_conn
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'error': '请先登录'}), 401

    kb = get_kb_by_id(kb_id)
    if not kb:
        return jsonify({'error': '知识库不存在'}), 404
    kb = dict(kb) if hasattr(kb, 'keys') else kb
    if not kb.get('rag_dataset_id'):
        return jsonify({'documents': [], 'total': 0})

    # 权限检查：需要 can_access 或更高权限
    role_names = get_user_roles(user_id)
    is_admin = 'admin' in role_names
    if not is_admin:
        with get_db_conn() as conn:
            c = conn.cursor()
            c.execute('SELECT role_id FROM roles WHERE role_name IN (%s)' %
                      ','.join(['?'] * len(role_names)), role_names)
            role_ids = [row['role_id'] for row in c.fetchall()]
        if not role_ids:
            return jsonify({'error': '无权限', 'code': 'forbidden'}), 403
        perms = get_kb_permissions_for_roles(role_ids)
        perm = perms.get(kb_id, {})
        if not perm.get('can_access'):
            return jsonify({'error': '无权限访问该知识库', 'code': 'forbidden'}), 403

    # RAG-Server KB
    from app.services.rag_kb_service import RAGServerKBService
    svc = RAGServerKBService(rag_dataset_id=kb['rag_dataset_id'], kb_name=kb.get('kb_name', ''))
    result = svc.list_documents()
    if 'error' in result:
        import logging
        logging.getLogger(__name__).error(f"[Admin] RAG list_documents error: {result['error']}")
    return jsonify(result)


# ==================== Permission Management ====================

@bp.route('/admin/permissions', methods=['POST'])
@admin_required
def update_permissions():
    action = request.form.get('action', 'set')
    role_id = request.form.get('role_id', type=int)

    # 禁止修改 admin 角色的权限
    if role_id == 1:
        flash('admin 角色拥有所有权限且不可被修改', 'error')
        return redirect(url_for('admin.roles'))

    if action == 'batch':
        # Batch update: submit all KB permissions for a role at once
        redirect_url = request.form.get('redirect', '/admin/roles')

        from app.models import get_all_kbs, set_kb_role_permission, remove_kb_role_permission
        kbs = get_all_kbs()

        for kb in kbs:
            kb_id = kb['kb_id']
            can_access = 1 if request.form.get(f'kb_access_{kb_id}') else 0
            can_edit = 1 if request.form.get(f'kb_edit_{kb_id}') else 0
            can_manage = 1 if request.form.get(f'kb_manage_{kb_id}') else 0

            if can_access or can_edit or can_manage:
                set_kb_role_permission(role_id, kb_id, can_access, can_edit, can_manage)
            else:
                remove_kb_role_permission(role_id, kb_id)

        flash('权限配置已保存', 'success')
        from flask import redirect
        return redirect(redirect_url)

    # Individual update (old single-permission mode)
    kb_id = request.form.get('kb_id', type=int)
    can_access = 1 if request.form.get('can_access') else 0
    can_edit = 1 if request.form.get('can_edit') else 0
    can_manage = 1 if request.form.get('can_manage') else 0

    from app.models import set_kb_role_permission, remove_kb_role_permission
    if action == 'remove':
        remove_kb_role_permission(role_id, kb_id)
        flash('权限已移除', 'success')
    else:
        set_kb_role_permission(role_id, kb_id, can_access, can_edit, can_manage)
        flash('权限已更新', 'success')

    from flask import redirect
    return redirect(url_for('admin.roles'))


# ==================== Model Listing ====================

@bp.route('/admin/kbs/models/<model_type>')
@admin_required
def get_model_list(model_type):
    """返回 embedding/rerank 模型列表（RAG-Server 模式，无 Dify）"""
    if model_type not in ('text-embedding', 'rerank'):
        return jsonify({'error': 'invalid model type'}), 400

    from app.config import get_embedding_config, get_reranker_config
    emb_cfg = get_embedding_config()
    rer_cfg = get_reranker_config()

    if model_type == 'text-embedding':
        # 优先从 RAG-Server config 读取，fallback 到 embedding config
        from app.config import get_rag_server_config
        rag_cfg = get_rag_server_config()
        if rag_cfg.get('enabled'):
            # RAG-Server 使用固定的 embedding 模型
            models = []
            if emb_cfg.get('provider') == 'siliconflow':
                models.append({'model_name': emb_cfg.get('siliconflow', {}).get('model', 'BAAI/bge-m3'), 'provider': 'siliconflow'})
            elif emb_cfg.get('provider') == 'ollama':
                models.append({'model_name': emb_cfg.get('ollama', {}).get('model', 'bge-m3:latest'), 'provider': 'ollama'})
            default_model = models[0]['model_name'] if models else None
            return jsonify({'models': models, 'default_model': default_model, 'default_rerank': None})

        # 无 RAG-Server 时返回空
        return jsonify({'models': [], 'default_model': None, 'default_rerank': None})

    else:  # rerank
        from app.config import get_rag_server_config
        rag_cfg = get_rag_server_config()
        if rag_cfg.get('enabled'):
            models = []
            if rer_cfg.get('provider') == 'siliconflow':
                models.append({'model_name': rer_cfg.get('siliconflow', {}).get('model', 'BAAI/bge-reranker-v2-m3'), 'provider': 'siliconflow'})
            default_rerank = models[0]['model_name'] if models else None
            return jsonify({'models': models, 'default_model': None, 'default_rerank': default_rerank})

        return jsonify({'models': [], 'default_model': None, 'default_rerank': None})


# ==================== Local QA KB Management ====================

@bp.route('/admin/kbs/<int:kb_id>/qa/upload', methods=['POST'])
@kb_edit_required
def upload_qa_csv(kb_id):
    """上传 CSV 文件导入问答对到本地问答知识库"""
    from app.models import get_kb_by_id, is_local_qa_kb
    if not is_local_qa_kb(kb_id):
        return jsonify({'error': '该知识库不是问答知识库'}), 400

    if 'file' not in request.files:
        return jsonify({'error': '未找到上传文件'}), 400
    file = request.files['file']
    if not file.filename:
        return jsonify({'error': '文件名为空'}), 400

    import csv, os, uuid, tempfile
    tmp_dir = tempfile.mkdtemp()
    tmp_path = os.path.join(tmp_dir, f'{uuid.uuid4().hex}.csv')
    file.save(tmp_path)

    try:
        qa_list = []
        with open(tmp_path, newline='', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                q = (row.get('问题') or row.get('question') or row.get('Q') or '').strip()
                a = (row.get('答案') or row.get('answer') or row.get('A') or '').strip()
                if not q:
                    continue
                qa_list.append({'question': q, 'answer': a})
    except Exception as e:
        os.remove(tmp_path)
        os.rmdir(tmp_dir)
        return jsonify({'error': f'CSV 解析失败: {e}'}), 500
    finally:
        os.remove(tmp_path)
        os.rmdir(tmp_dir)

    if not qa_list:
        return jsonify({'error': '未找到有效问答对（需要 question/问题 和 answer/答案 列）'}), 400

    from app.services.local_qa import add_local_qa_items
    result = add_local_qa_items(kb_id, qa_list)
    return jsonify({'ok': True, **result})


@bp.route('/admin/kbs/<int:kb_id>/qa/add', methods=['POST'])
@kb_edit_required
def add_qa_item(kb_id):
    """手动添加单条问答对"""
    from app.models import is_local_qa_kb
    if not is_local_qa_kb(kb_id):
        return jsonify({'error': '该知识库不是问答知识库'}), 400

    data = request.get_json() or {}
    q = (data.get('question') or '').strip()
    a = (data.get('answer') or '').strip()
    if not q:
        return jsonify({'error': '问题不能为空'}), 400

    from app.services.local_qa import add_local_qa_items
    result = add_local_qa_items(kb_id, [{'question': q, 'answer': a}])
    return jsonify({'ok': True, **result})


@bp.route('/admin/kbs/<int:kb_id>/qa/items', methods=['GET'])
def list_qa_items(kb_id):
    """列出问答知识库所有问答对（支持分页）"""
    from app.models import get_kb_by_id, is_local_qa_kb, get_kb_permissions_for_roles, get_user_roles, get_db_conn
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'error': '请先登录'}), 401

    kb = get_kb_by_id(kb_id)
    if not kb:
        return jsonify({'error': '知识库不存在'}), 404

    # 权限检查
    role_names = get_user_roles(user_id)
    is_admin = 'admin' in role_names
    if not is_admin:
        with get_db_conn() as conn:
            c = conn.cursor()
            c.execute('SELECT role_id FROM roles WHERE role_name IN (%s)' %
                      ','.join(['?'] * len(role_names)), role_names)
            role_ids = [row['role_id'] for row in c.fetchall()]
        if not role_ids:
            return jsonify({'error': '无权限', 'code': 'forbidden'}), 403
        perms = get_kb_permissions_for_roles(role_ids)
        perm = perms.get(kb_id, {})
        if not perm.get('can_access'):
            return jsonify({'error': '无权限访问该知识库', 'code': 'forbidden'}), 403

    if not is_local_qa_kb(kb_id):
        return jsonify({'error': '该知识库不是问答知识库'}), 400

    from app.services.local_qa import list_local_qa_items
    page = max(1, request.args.get('page', 1, type=int))
    limit = min(200, max(10, request.args.get('limit', 50, type=int)))
    offset = (page - 1) * limit
    items, total = list_local_qa_items(kb_id, offset=offset, limit=limit)
    return jsonify({'items': items, 'total': total, 'page': page, 'limit': limit})


@bp.route('/admin/kbs/<int:kb_id>/qa/delete/<int:item_id>', methods=['POST'])
@bp.route('/admin/kbs/<int:kb_id>/qa/<int:item_id>', methods=['DELETE'])
@kb_edit_required
def delete_qa_item(kb_id, item_id=None):
    """删除指定问答对"""
    # Support both /qa/delete/{id} (POST) and /qa/{id} (DELETE)
    from app.models import is_local_qa_kb
    if not is_local_qa_kb(kb_id):
        return jsonify({'error': '该知识库不是问答知识库'}), 400

    from app.services.local_qa import delete_local_qa_item
    delete_local_qa_item(kb_id, item_id)
    return jsonify({'ok': True})


@bp.route('/admin/kbs/<int:kb_id>/qa/clear', methods=['POST'])
@kb_edit_required
def clear_qa_items(kb_id):
    """清空问答知识库所有内容"""
    from app.models import is_local_qa_kb
    if not is_local_qa_kb(kb_id):
        return jsonify({'error': '该知识库不是问答知识库'}), 400

    from app.services.local_qa import clear_local_qa
    clear_local_qa(kb_id)
    return jsonify({'ok': True})


@bp.route('/admin/kbs/<int:kb_id>/qa/count', methods=['GET'])
def qa_count(kb_id):
    """获取问答知识库的问答对数量"""
    from app.models import is_local_qa_kb
    if not is_local_qa_kb(kb_id):
        return jsonify({'error': '该知识库不是问答知识库'}), 400

    from app.services.local_qa import count_local_qa
    count = count_local_qa(kb_id)
    return jsonify({'count': count})


@bp.route('/admin/kbs/<int:kb_id>/qa/index-status', methods=['GET'])
def qa_index_status(kb_id):
    """获取问答知识库的向量索引状态"""
    from app.models import is_local_qa_kb
    if not is_local_qa_kb(kb_id):
        return jsonify({'error': '该知识库不是问答知识库'}), 400

    from app.services.local_qa import is_indexed
    indexed = is_indexed(kb_id)
    return jsonify({'indexed': indexed})



@bp.route('/admin/kbs/<int:kb_id>/qa/rebuild-index', methods=['POST'])
@kb_edit_required
def qa_rebuild_index(kb_id):
    """重建问答知识库的向量索引"""
    from app.models import is_local_qa_kb
    if not is_local_qa_kb(kb_id):
        return jsonify({'error': '该知识库不是问答知识库'}), 400

    from app.services.local_qa import rebuild_local_qa_index
    result = rebuild_local_qa_index(kb_id)
    if result.get('error'):
        return jsonify({'error': result['error']}), 500
    return jsonify({'ok': True, 'indexed_count': result.get('indexed_count', 0)})


# ==================== Document Download ====================

@bp.route('/admin/kbs/<int:kb_id>/documents/<doc_id>/download')
@kb_edit_required
def download_single_document(kb_id, doc_id):
    """
    下载知识库中的单个文档原文。
    Dify API: GET /v1/datasets/{dataset_id}/documents/{document_id}/download
    返回文件流。
    """
    from flask import request
    from app.models import get_kb_by_id, is_local_qa_kb
    kb = get_kb_by_id(kb_id)
    if not kb:
        return '知识库不存在', 404
    kb = dict(kb)
    if is_local_qa_kb(kb_id):
        return '问答知识库无单独文件可下载', 400

    # 优先用前端传来的文件名（Dify doc.name），其次 fallback
    raw_name = request.args.get('filename', '')
    if raw_name:
        import urllib.parse
        filename = urllib.parse.unquote(raw_name)
    else:
        filename = None

    # RAG-Server KB
    if kb.get('rag_dataset_id'):
        from app.services.rag_kb_service import RAGServerKBService
        svc = RAGServerKBService(rag_dataset_id=kb['rag_dataset_id'], kb_name=kb.get('kb_name', ''))
        content, suggested = svc.download_document(doc_id, filename=filename)
        if isinstance(content, dict) and content.get('error'):
            return content['error'], 500
        import urllib.parse
        final_name = filename or suggested or 'document'
        encoded = urllib.parse.quote(final_name, safe='')
        from flask import Response
        return Response(
            content,
            mimetype='application/octet-stream',
            headers={
                'Content-Disposition': f"attachment; filename={encoded}; filename*=UTF-8''{encoded}",
                'Content-Length': str(len(content)),
            },
        )

    return '知识库不存在', 404


@bp.route('/admin/kbs/<int:kb_id>/documents/download-all', methods=['POST'])
@kb_edit_required
def download_all_documents(kb_id):
    """
    打包下载知识库所有文档（ZIP）。
    如果文档数 <= 100，直接调 Dify/RAG download-zip 接口；
    如果 > 100，分批调用后本地合并为单个 ZIP。
    """
    from app.models import get_kb_by_id, is_local_qa_kb
    kb = get_kb_by_id(kb_id)
    if not kb:
        return jsonify({'error': '知识库不存在'}), 404
    kb = dict(kb)
    if is_local_qa_kb(kb_id):
        return jsonify({'error': '问答知识库请使用「导出问答对」功能'}), 400

    # RAG-Server KB
    if kb.get('rag_dataset_id'):
        from app.services.rag_kb_service import RAGServerKBService
        svc = RAGServerKBService(rag_dataset_id=kb['rag_dataset_id'], kb_name=kb.get('kb_name', ''))
        doc_list = svc.list_documents()
        if 'error' in doc_list:
            return jsonify({'error': doc_list['error']}), 500
        docs = doc_list.get('documents', [])
        if not docs:
            return jsonify({'error': '知识库中没有文档'}), 400
        doc_ids = [d['id'] for d in docs]
        kb_name = kb.get('kb_name', 'knowledge_base')
        zip_bytes, zip_name = svc.download_documents_zip(doc_ids, kb_name)
        if isinstance(zip_bytes, dict) and zip_bytes.get('error'):
            return jsonify({'error': zip_bytes['error']}), 500
        from flask import Response
        return Response(
            zip_bytes,
            mimetype='application/zip',
            headers={
                'Content-Disposition': f"attachment; filename*=UTF-8''{zip_name}",
                'Content-Length': str(len(zip_bytes)),
            },
        )

    return '知识库不存在', 404


# ==================== Download All KBs ====================

@bp.route('/admin/kbs/download-all-zip', methods=['POST'])
@admin_required
def download_all_kbs_zip():
    """
    打包下载所有知识库（文档知识库 + 问答知识库）。
    返回单一 ZIP，按知识库名称建文件夹。
    """
    import zipfile, io, csv
    from app.models import get_all_kbs, get_kb_by_id, is_local_qa_kb

    master = io.BytesIO()
    with zipfile.ZipFile(master, 'w', zipfile.ZIP_DEFLATED) as mz:
        kbs = get_all_kbs()
        for kb in kbs:
            kb_id = kb['kb_id']
            kb_name = (kb.get('kb_name') or f'kb_{kb_id}').replace('/', '_').replace('\\', '_')
            folder = kb_name

            if is_local_qa_kb(kb_id):
                # 导出问答对 CSV
                from app.services.local_qa import list_local_qa_items
                items, total = list_local_qa_items(kb_id, offset=0, limit=999999)
                buf = io.StringIO()
                writer = csv.writer(buf)
                writer.writerow(['问题', '答案'])
                for it in items:
                    writer.writerow([it.get('question', ''), it.get('answer', '')])
                mz.writestr(f'{folder}/qa_export.csv', buf.getvalue().encode('utf-8'))
            elif kb.get('rag_dataset_id'):
                # 文档知识库：通过 RAG-Server 下载
                from app.services.rag_kb_service import RAGServerKBService
                svc = RAGServerKBService(rag_dataset_id=kb['rag_dataset_id'], kb_name=kb_name)
                doc_result = svc.list_documents()
                if 'error' in doc_result:
                    continue
                docs = doc_result.get('documents', [])
                if not docs:
                    continue
                doc_ids = [d['id'] for d in docs]
                BATCH = 100
                for i in range(0, len(doc_ids), BATCH):
                    batch = doc_ids[i:i + BATCH]
                    zip_bytes, _ = svc.download_documents_zip(batch, kb_name)
                    if isinstance(zip_bytes, dict) and zip_bytes.get('error'):
                        continue
                    try:
                        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
                        for item in zf.infolist():
                            data = zf.read(item.filename)
                            fname = item.filename.split('/')[-1] if '/' in item.filename else item.filename
                            if fname:
                                mz.writestr(f'{folder}/{fname}', data)
                        zf.close()
                    except Exception:
                        continue

    master.seek(0)
    from flask import Response
    return Response(
        master.getvalue(),
        mimetype='application/zip',
        headers={
            'Content-Disposition': "attachment; filename*=UTF-8''all_knowledge_bases.zip",
        },
    )


# ==================== QA Export ====================

@bp.route('/admin/kbs/<int:kb_id>/qa/export')
@kb_edit_required
def export_qa_csv(kb_id):
    """
    导出问答知识库所有问答对为 CSV 文件。
    文件名: {kb_name}_qa_export.csv
    """
    from app.models import is_local_qa_kb
    if not is_local_qa_kb(kb_id):
        return jsonify({'error': '该知识库不是问答知识库'}), 400

    from app.models import get_kb_by_id
    kb = get_kb_by_id(kb_id)
    kb = dict(kb)
    kb_name = (kb.get('kb_name') or 'qa_export').replace('/', '_').replace('\\', '_')

    from app.services.local_qa import list_local_qa_items
    items, total = list_local_qa_items(kb_id, offset=0, limit=999999)

    import csv, io
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(['问题', '答案'])
    for it in items:
        writer.writerow([it.get('question', ''), it.get('answer', '')])

    from flask import Response
    return Response(
        buf.getvalue(),
        mimetype='text/csv; charset=utf-8',
        headers={
            'Content-Disposition': f"attachment; filename*=UTF-8''{kb_name}_qa_export.csv",
        },
    )