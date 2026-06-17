"""
Dify sync service - automatically sync knowledge bases from Dify API to local DB
"""
import requests
import logging

logger = logging.getLogger(__name__)


def _doc_form_to_template(doc_form):
    """将 Dify doc_form 转换为本地 template_type"""
    if doc_form == 'hierarchical_model':
        return 'hierarchical_full'
    # text_model, paragraph, page, or None → default to text_plain
    return 'text_plain'


def sync_kbs_from_dify():
    """
    Fetch all datasets from Dify API and sync to local kb_configs table.
    - Create new KBs if not exist locally
    - Delete local KBs that no longer exist in Dify (auto cleanup)
    Returns (created_count, deleted_count, errors)
    """
    from app.config import get_dify_defaults
    from app.models import get_db, get_role_by_name, set_kb_role_permission

    dify = get_dify_defaults()
    api_url = dify.get('api_url', '').rstrip('/')
    api_key = dify.get('api_key', '')

    if not api_url or not api_key:
        logger.warn("[Sync] Dify config is empty, skipping sync")
        return 0, 0, ['Dify api_url or api_key not configured']

    logger.info(f"[Sync] Starting Dify sync | url={api_url}")

    try:
        resp = requests.get(
            f'{api_url}/datasets',
            headers={'Authorization': f'Bearer {api_key}'},
            timeout=30,
        )
        logger.info(f"[Sync] Dify API responded with HTTP {resp.status_code}")

        if resp.status_code != 200:
            err_msg = f"HTTP {resp.status_code}: {resp.text[:200]}"
            logger.error(f"[Sync] Failed: {err_msg}")
            return 0, 0, [err_msg]

        data = resp.json()
        datasets = data.get('data', [])
        remote_ids = set(ds['id'] for ds in datasets if ds.get('id'))

        if not datasets:
            logger.info("[Sync] No datasets found in Dify, will clean up orphaned local KBs")
            remote_ids = set()

        conn = get_db()
        c = conn.cursor()

        # 1. Clean up local KBs that no longer exist in Dify
        # Skip local QA KBs (template_type='qa' or dify_dataset_id IS NULL)
        c.execute('SELECT kb_id, dify_dataset_id, kb_name, template_type FROM kb_configs')
        local_kbs = list(c.fetchall())
        deleted_local = 0
        for row in local_kbs:
            local_id = row['dify_dataset_id']
            # Skip local QA KBs: no dify_dataset_id or explicit qa type
            template = row['template_type'] if 'template_type' in row.keys() else None
            if template == 'qa' or (local_id is None and template == 'qa'):
                logger.info(f"[Sync] Skipping local QA KB: {row['kb_name']} (id={row['kb_id']})")
                continue
            if local_id not in remote_ids:
                # Delete role permissions first, then the KB
                c.execute('DELETE FROM role_kb_permissions WHERE kb_id = ?', (row['kb_id'],))
                c.execute('DELETE FROM kb_configs WHERE kb_id = ?', (row['kb_id'],))
                logger.info(f"[Sync] Removed orphaned KB: {row['kb_name']} (dataset_id={local_id})")
                deleted_local += 1

        created = 0
        errors = []

        for ds in datasets:
            kb_name = ds.get('name', '未知知识库')
            description = ds.get('description', '') or ''
            dataset_id = ds.get('id', '')
            if not dataset_id:
                continue

            try:
                c.execute('SELECT kb_id FROM kb_configs WHERE dify_dataset_id = ?', (dataset_id,))
                row = c.fetchone()

                if row:
                    c.execute('''
                        UPDATE kb_configs
                        SET kb_name = ?, description = ?, dify_api_url = ?, dify_api_key = ?, template_type = ?
                        WHERE dify_dataset_id = ?
                    ''', (kb_name, description, api_url, api_key, _doc_form_to_template(ds.get('doc_form')), dataset_id))
                    kb_id = row['kb_id']
                    logger.info(f"[Sync] Updated KB: {kb_name} (kb_id={kb_id})")

                    # 确保 admin/viewer 有权限（INSERT OR REPLACE 保证幂等）
                    admin_role = get_role_by_name('admin')
                    if admin_role:
                        c.execute('''
                            INSERT OR REPLACE INTO role_kb_permissions (role_id, kb_id, can_access, can_edit, can_manage)
                            VALUES (?, ?, 1, 1, 1)
                        ''', (admin_role['role_id'], kb_id))
                    viewer_role = get_role_by_name('viewer')
                    if viewer_role:
                        c.execute('''
                            INSERT OR REPLACE INTO role_kb_permissions (role_id, kb_id, can_access, can_edit, can_manage)
                            VALUES (?, ?, 1, 1, 1)
                        ''', (viewer_role['role_id'], kb_id))
                else:
                    c.execute('''
                        INSERT INTO kb_configs (kb_name, description, dify_api_url, dify_api_key, dify_dataset_id, template_type, is_active)
                        VALUES (?, ?, ?, ?, ?, ?, 1)
                    ''', (kb_name, description, api_url, api_key, dataset_id, _doc_form_to_template(ds.get('doc_form'))))
                    kb_id = c.lastrowid

                    # 直接用同一个连接写入权限，不开新连接（避免 SQLite 并发锁定）
                    admin_role = get_role_by_name('admin')
                    if admin_role:
                        c.execute('''
                            INSERT OR REPLACE INTO role_kb_permissions (role_id, kb_id, can_access, can_edit, can_manage)
                            VALUES (?, ?, 1, 1, 1)
                        ''', (admin_role['role_id'], kb_id))
                    viewer_role = get_role_by_name('viewer')
                    if viewer_role:
                        c.execute('''
                            INSERT OR REPLACE INTO role_kb_permissions (role_id, kb_id, can_access, can_edit, can_manage)
                            VALUES (?, ?, 1, 1, 1)
                        ''', (viewer_role['role_id'], kb_id))

                    created += 1
                    logger.info(f"[Sync] Created KB: {kb_name} (Dataset ID: {dataset_id[:20]}...)")

            except Exception as e:
                err_msg = f"KB {kb_name}: {e}"
                logger.error(f"[Sync] {err_msg}")
                errors.append(err_msg)

        conn.commit()
        conn.close()
        logger.info(f"[Sync] Done | created={created}, deleted={deleted_local}, errors={len(errors)}")
        return created, deleted_local, errors

    except requests.exceptions.Timeout:
        err_msg = "Dify API timeout"
        logger.error(f"[Sync] {err_msg}")
        return 0, 0, [err_msg]
    except Exception as e:
        err_msg = str(e)
        logger.error(f"[Sync] Exception: {err_msg}")
        return 0, 0, [err_msg]


def auto_sync_on_startup():
    """Called at app startup to sync KBs from Dify"""
    logger.info("[Sync] Running startup sync from Dify...")
    created, deleted, errors = sync_kbs_from_dify()
    if errors:
        logger.warn(f"[Sync] Startup sync completed with errors: {errors}")
    else:
        logger.info(f"[Sync] Startup sync complete: {created} new, {deleted} removed")