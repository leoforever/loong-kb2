#!/usr/bin/env python3
"""
初始化脚本：创建 admin 用户、Dify 知识库配置、分配权限

所有配置从 config.yaml 读取，不再硬编码 IP/端口。
"""
import bcrypt
import sys
import os
from pathlib import Path

sys.path.insert(0, '/root/.openclaw/workspace/loong-kb')

# 读取 config.yaml
CONFIG_PATH = Path(__file__).parent / 'config.yaml'

def load_config():
    import yaml
    with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f) or {}

cfg = load_config()

# 服务器地址（用于显示）
srv = cfg.get('server', {})
_server_host = srv.get('host', '0.0.0.0')
_server_port = srv.get('port', 5001)

# 动态计算可访问地址（0.0.0.0 时显示真实 IP）
if _server_host == '0.0.0.0':
    _display_ip = os.popen("hostname -I 2>/dev/null | awk '{print $1}'").read().strip() or 'localhost'
else:
    _display_ip = _server_host

ACCESS_URL = f"http://{_display_ip}:{_server_port}"

# Dify 默认配置（来自 config.yaml）
dify_cfg = cfg.get('dify', {})
DIFY_API_URL = dify_cfg.get('api_url', 'http://localhost/v1').rstrip('/').replace('/v1', '')
DIFY_API_KEY = dify_cfg.get('api_key', '')


def setup():
    from app.models import init_db, create_user, create_kb, assign_role_to_user, set_kb_role_permission
    from app.models import get_role_by_name, get_kb_by_id, get_db_conn

    init_db()
    print("✓ 数据库初始化完成")

    # 兼容旧数据库：若 kb_configs 缺少 template_type 列则添加
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute("PRAGMA table_info(kb_configs)")
        cols = [r['name'] for r in c.fetchall()]
        if 'template_type' not in cols:
            c.execute("ALTER TABLE kb_configs ADD COLUMN template_type TEXT")
            print("✓ 添加 template_type 列到 kb_configs")
        c.execute("PRAGMA table_info(role_kb_permissions)")
        perm_cols = [r['name'] for r in c.fetchall()]
        if 'can_access' not in perm_cols:
            # 旧数据库：can_read → can_access, can_query → can_edit/can_manage
            c.execute("ALTER TABLE role_kb_permissions ADD COLUMN can_access INTEGER DEFAULT 0")
            c.execute("ALTER TABLE role_kb_permissions ADD COLUMN can_edit INTEGER DEFAULT 0")
            c.execute("ALTER TABLE role_kb_permissions ADD COLUMN can_manage INTEGER DEFAULT 0")
            print("✓ 添加 can_access/can_edit/can_manage 列到 role_kb_permissions")
            # 迁移旧数据
            if 'can_read' in perm_cols and 'can_query' in perm_cols:
                c.execute("UPDATE role_kb_permissions SET can_access = COALESCE(can_read, 0), can_edit = COALESCE(can_query, 0), can_manage = COALESCE(can_query, 0) WHERE can_read = 1 OR can_query = 1")
                print("✓ 迁移旧权限数据（can_read→can_access, can_query→can_edit/can_manage）")

    # Create admin user
    pwd_hash = bcrypt.hashpw('admin123'.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
    try:
        user_id = create_user('admin', pwd_hash, '管理员')
        print(f"✓ 创建管理员用户: admin / admin123")
    except Exception as e:
        print(f"  admin 用户已存在，跳过: {e}")
        from app.models import get_user_by_username
        user_id = get_user_by_username('admin')['user_id']

    # Assign admin role
    admin_role = get_role_by_name('admin')
    assign_role_to_user(user_id, admin_role['role_id'])
    print(f"✓ 分配 admin 角色")

    # Create viewer role if not exists
    viewer_role = get_role_by_name('viewer')
    if not viewer_role:
        with get_db_conn() as conn:
            c = conn.cursor()
            c.execute("INSERT INTO roles (role_name, description) VALUES ('viewer', '普通用户，只读访问')")
        viewer_role = get_role_by_name('viewer')
        print(f"✓ 创建 viewer 角色")

    # Add Dify KB config — 使用 config.yaml 中的默认值
    try:
        kb_id = create_kb(
            name='龙芯产品手册',
            description='龙芯 2K3000 处理器用户手册 V0.9 试用版',
            dify_api_url=DIFY_API_URL,
            dify_api_key=DIFY_API_KEY,
            dify_dataset_id='',
        )
        print(f"✓ 创建知识库: 龙芯产品手册 (id={kb_id})")
    except Exception as e:
        print(f"  知识库已存在，跳过: {e}")
        from app.models import get_all_kbs
        kbs = get_all_kbs()
        kb_id = kbs[0]['kb_id'] if kbs else None

    if kb_id:
        set_kb_role_permission(admin_role['role_id'], kb_id, can_access=1, can_edit=1, can_manage=1)
        set_kb_role_permission(viewer_role['role_id'], kb_id, can_access=1, can_edit=1, can_manage=1)
        print(f"✓ 权限配置完成: admin(viewer) -> 龙芯产品手册")

    print("\n✅ 初始化完成!")
    print("=" * 40)
    print(f"  管理员账号: admin / admin123")
    print(f"  访问地址: {ACCESS_URL}")
    print(f"  知识库: 龙芯产品手册 (对接 Dify API)")
    print(f"  Dify API: {DIFY_API_URL}")

if __name__ == '__main__':
    setup()