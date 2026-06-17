"""
Database models and initialization
Includes: users, roles, kb_configs, user_roles, kb_roles
"""
import sqlite3
import os
import logging
from contextlib import contextmanager

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, 'cache', 'db.sqlite')


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def get_db_conn():
    conn = get_db()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """Initialize all tables"""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with get_db_conn() as conn:
        c = conn.cursor()

        # Roles table
        c.execute('''
            CREATE TABLE IF NOT EXISTS roles (
                role_id INTEGER PRIMARY KEY AUTOINCREMENT,
                role_name TEXT UNIQUE NOT NULL,
                description TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Users table
        c.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                display_name TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # User <-> Role mapping
        c.execute('''
            CREATE TABLE IF NOT EXISTS user_roles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                role_id INTEGER NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE,
                FOREIGN KEY (role_id) REFERENCES roles(role_id) ON DELETE CASCADE,
                UNIQUE(user_id, role_id)
            )
        ''')

        # Knowledge base configs (points to external Dify API or local QA)
        # Note: dify_api_url/key/dataset_id allow NULL for local QA KBs
        c.execute('''
            CREATE TABLE IF NOT EXISTS kb_configs (
                kb_id INTEGER PRIMARY KEY AUTOINCREMENT,
                kb_name TEXT NOT NULL,
                description TEXT,
                dify_api_url TEXT,
                dify_api_key TEXT,
                dify_dataset_id TEXT,
                template_type TEXT DEFAULT 'dify',
                is_active INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Migrate old kb_configs: if old table has NOT NULL on dify_* columns,
        # it can't store QA KBs. We detect this by trying to insert a NULL.
        # Strategy: rename old table -> create new schema -> copy data back.
        try:
            # Test if old schema allows NULL for dify_dataset_id
            c.execute("INSERT INTO kb_configs (kb_name, dify_dataset_id) VALUES ('__test__', NULL)")
            c.execute("DELETE FROM kb_configs WHERE kb_name = '__test__'")
            logger.info("[DB Migrate] kb_configs schema already compatible")
        except Exception:
            # Old schema has NOT NULL - need to migrate
            try:
                # 1. Rename old table
                c.execute("ALTER TABLE kb_configs RENAME TO _kb_configs_old")
                # 2. Create new table with nullable columns
                c.execute('''
                    CREATE TABLE kb_configs (
                        kb_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        kb_name TEXT NOT NULL,
                        description TEXT,
                        dify_api_url TEXT,
                        dify_api_key TEXT,
                        dify_dataset_id TEXT,
                        template_type TEXT DEFAULT 'dify',
                        is_active INTEGER DEFAULT 1,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                ''')
                # 3. Copy data (template_type from old rows will be NULL -> default to 'dify')
                c.execute("INSERT INTO kb_configs (kb_id, kb_name, description, dify_api_url, dify_api_key, dify_dataset_id, is_active, created_at) SELECT kb_id, kb_name, description, dify_api_url, dify_api_key, dify_dataset_id, is_active, created_at FROM _kb_configs_old")
                # 4. Drop old table
                c.execute("DROP TABLE _kb_configs_old")
                logger.info("[DB Migrate] kb_configs migrated: NOT NULL constraints removed")
            except Exception as e2:
                # Fallback: try to recover
                try:
                    c.execute("ALTER TABLE kb_configs RENAME TO _kb_configs_broken")
                except Exception:
                    pass
                logger.error(f"[DB Migrate] kb_configs migration failed: {e2}")

        c.execute('''
            CREATE TABLE IF NOT EXISTS role_kb_permissions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                role_id INTEGER NOT NULL,
                kb_id INTEGER NOT NULL,
                can_access INTEGER DEFAULT 0,
                can_edit INTEGER DEFAULT 0,
                can_manage INTEGER DEFAULT 0,
                FOREIGN KEY (role_id) REFERENCES roles(role_id) ON DELETE CASCADE,
                FOREIGN KEY (kb_id) REFERENCES kb_configs(kb_id) ON DELETE CASCADE,
                UNIQUE(role_id, kb_id)
            )
        ''')

        # Query history
        c.execute('''
            CREATE TABLE IF NOT EXISTS query_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                kb_id INTEGER,
                question TEXT NOT NULL,
                answer TEXT NOT NULL,
                hit_cache INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE,
                FOREIGN KEY (kb_id) REFERENCES kb_configs(kb_id) ON DELETE SET NULL
            )
        ''')

        # Add template_type column to existing kb_configs (may not exist on old schemas)
        try:
            c.execute("ALTER TABLE kb_configs ADD COLUMN template_type TEXT DEFAULT 'dify'")
        except Exception:
            pass  # column already exists

        c.execute('CREATE INDEX IF NOT EXISTS idx_user_roles_user ON user_roles(user_id)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_user_roles_role ON user_roles(role_id)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_role_kb_role ON role_kb_permissions(role_id)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_query_log_user ON query_log(user_id)')

    logger.info(f"Database initialized: {DB_PATH}")


# ==============================
# User operations
# ==============================

def create_user(username, password_hash, display_name=None):
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute(
            'INSERT INTO users (username, password_hash, display_name) VALUES (?, ?, ?)',
            (username, password_hash, display_name or username)
        )
        return c.lastrowid


def get_user_by_username(username):
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute('SELECT * FROM users WHERE username = ?', (username,))
        return c.fetchone()


def _row(row):
    """Convert sqlite3.Row to dict (sqlite3.Row doesn't support .get())"""
    return dict(row)


def get_user_by_id(user_id):
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute('SELECT * FROM users WHERE user_id = ?', (user_id,))
        return c.fetchone()


def get_all_users():
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute('''
            SELECT u.*, GROUP_CONCAT(r.role_name) as roles
            FROM users u
            LEFT JOIN user_roles ur ON u.user_id = ur.user_id
            LEFT JOIN roles r ON ur.role_id = r.role_id
            GROUP BY u.user_id
        ''')
        return c.fetchall()


def get_user_roles(user_id):
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute('''
            SELECT r.* FROM roles r
            JOIN user_roles ur ON r.role_id = ur.role_id
            WHERE ur.user_id = ?
        ''', (user_id,))
        return [row['role_name'] for row in c.fetchall()]


# ==============================
# Role operations
# ==============================

def get_all_roles():
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute('SELECT * FROM roles ORDER BY role_id')
        return c.fetchall()


def get_role_by_name(name):
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute('SELECT * FROM roles WHERE role_name = ?', (name,))
        return c.fetchone()


def assign_role_to_user(user_id, role_id):
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute('INSERT OR IGNORE INTO user_roles (user_id, role_id) VALUES (?, ?)', (user_id, role_id))


def remove_user_role(user_id, role_id):
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute('DELETE FROM user_roles WHERE user_id = ? AND role_id = ?', (user_id, role_id))


# ==============================
# KB config operations
# ==============================

def create_kb(name, description, dify_api_url, dify_api_key, dify_dataset_id, template_type=None):
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute('''
            INSERT INTO kb_configs (kb_name, description, dify_api_url, dify_api_key, dify_dataset_id, template_type)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (name, description, dify_api_url, dify_api_key, dify_dataset_id, template_type))
        return c.lastrowid


def get_all_kbs():
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute('SELECT * FROM kb_configs ORDER BY kb_id')
        return [_row(r) for r in c.fetchall()]


def get_kb_by_id(kb_id):
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute('SELECT * FROM kb_configs WHERE kb_id = ?', (kb_id,))
        return c.fetchone()


def update_kb(kb_id, name, description, dify_api_url, dify_api_key, dify_dataset_id):
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute('''
            UPDATE kb_configs
            SET kb_name=?, description=?, dify_api_url=?, dify_api_key=?, dify_dataset_id=?
            WHERE kb_id=?
        ''', (name, description, dify_api_url, dify_api_key, dify_dataset_id, kb_id))


def delete_kb(kb_id):
    # 清理 FAISS 文件（如果是本地问答库）
    from app.services.local_qa import delete_local_qa_kb
    delete_local_qa_kb(kb_id)
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute('DELETE FROM role_kb_permissions WHERE kb_id = ?', (kb_id,))
        c.execute('DELETE FROM kb_configs WHERE kb_id = ?', (kb_id,))


# ==============================
# KB permission operations
# ==============================

def set_kb_role_permission(role_id, kb_id, can_access=0, can_edit=0, can_manage=0):
    """设置角色对知识库的权限。上级权限自动包含下级权限：manage → edit → access"""
    # Enforce inheritance: manage → edit → access
    if can_manage:
        can_edit = 1
        can_access = 1
    elif can_edit:
        can_access = 1
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute('''
            INSERT OR REPLACE INTO role_kb_permissions (role_id, kb_id, can_access, can_edit, can_manage)
            VALUES (?, ?, ?, ?, ?)
        ''', (role_id, kb_id, can_access, can_edit, can_manage))


def remove_kb_role_permission(role_id, kb_id):
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute('DELETE FROM role_kb_permissions WHERE role_id = ? AND kb_id = ?', (role_id, kb_id))


def get_kb_permissions_for_roles(role_ids):
    """Return dict of kb_id -> {can_access, can_edit, can_manage} for given role_ids"""
    if not role_ids:
        return {}
    with get_db_conn() as conn:
        c = conn.cursor()
        placeholders = ','.join(['?'] * len(role_ids))
        c.execute(f'''
            SELECT kb_id, MAX(can_access) as can_access, MAX(can_edit) as can_edit, MAX(can_manage) as can_manage
            FROM role_kb_permissions
            WHERE role_id IN ({placeholders})
            GROUP BY kb_id
        ''', role_ids)
        return {row['kb_id']: {'can_access': row['can_access'], 'can_edit': row['can_edit'], 'can_manage': row['can_manage']}
                for row in c.fetchall()}


def get_role_kb_permissions(role_id):
    """Get all KB permissions for a specific role"""
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute('''
            SELECT rkp.*, kc.kb_name, kc.dify_dataset_id
            FROM role_kb_permissions rkp
            JOIN kb_configs kc ON rkp.kb_id = kc.kb_id
            WHERE rkp.role_id = ?
        ''', (role_id,))
        return c.fetchall()


def get_all_role_kb_permissions():
    """Get all role-KB permission mappings for admin view"""
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute('''
            SELECT rkp.*, kc.kb_name, r.role_name
            FROM role_kb_permissions rkp
            JOIN kb_configs kc ON rkp.kb_id = kc.kb_id
            JOIN roles r ON rkp.role_id = r.role_id
        ''')
        return c.fetchall()


# ==============================
# Query log
# ==============================

def save_query_log(user_id, kb_id, question, answer, hit_cache=0):
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute('''
            INSERT INTO query_log (user_id, kb_id, question, answer, hit_cache)
            VALUES (?, ?, ?, ?, ?)
        ''', (user_id, kb_id, question, answer, hit_cache))


def get_user_history(user_id, limit=50):
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute('''
            SELECT ql.*, kc.kb_name
            FROM query_log ql
            LEFT JOIN kb_configs kc ON ql.kb_id = kc.kb_id
            WHERE ql.user_id = ?
            ORDER BY ql.created_at DESC
            LIMIT ?
        ''', (user_id, limit))
        return c.fetchall()


def delete_user_history(user_id):
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute('DELETE FROM query_log WHERE user_id = ?', (user_id,))


# ==============================
# Local QA KB operations
# ==============================

def create_local_qa_kb(name, description=None):
    """创建本地问答知识库（不走 Dify）"""
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute('''
            INSERT INTO kb_configs (kb_name, description, dify_api_url, dify_api_key, dify_dataset_id, template_type)
            VALUES (?, ?, NULL, NULL, NULL, ?)
        ''', (name, description or '', 'qa'))
        return c.lastrowid


def is_local_qa_kb(kb_id):
    """判断指定 KB 是否为本地问答知识库"""
    kb = get_kb_by_id(kb_id)
    if not kb:
        return False
    template = kb['template_type'] if 'template_type' in kb.keys() else None
    return template == 'qa'
