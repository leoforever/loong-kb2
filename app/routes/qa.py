"""
QA routes - user knowledge base Q&A
"""
import json
import logging
import uuid
import threading
import queue
from flask import Blueprint, request, render_template, jsonify, g, session, Response, stream_with_context

from app.models import get_user_roles, get_kb_permissions_for_roles, get_db_conn, get_all_kbs, save_query_log

bp = Blueprint('qa', __name__)
logger = logging.getLogger(__name__)


def get_user_accessible_kbs(user_id):
    """Return list of kb_configs that the user can access (via their roles)"""
    from app.models import get_user_roles, get_all_kbs, get_kb_permissions_for_roles
    role_names = get_user_roles(user_id)
    if not role_names:
        return []

    from app.models import get_db_conn
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute('SELECT role_id FROM roles WHERE role_name IN (%s)' %
                  ','.join(['?'] * len(role_names)), role_names)
        role_ids = [row['role_id'] for row in c.fetchall()]

    perms = get_kb_permissions_for_roles(role_ids)
    all_kbs = get_all_kbs()
    accessible = [kb for kb in all_kbs if perms.get(kb['kb_id'], {}).get('can_access')]
    return accessible


# In-memory conversation history per user_id (persists across page refreshes in same session)
_conversation_cache = {}  # {user_id: [messages]}


def _load_history(user_id):
    """Load conversation history from DB into memory"""
    if user_id in _conversation_cache:
        return
    from app.models import get_user_history
    history = get_user_history(user_id, limit=100)
    # Build messages list from history (oldest first)
    msgs = []
    for row in reversed(history):
        msgs.append({'role': 'user', 'content': row['question']})
        msgs.append({'role': 'assistant', 'content': row['answer']})
    _conversation_cache[user_id] = msgs


@bp.route('/qa')
def index():
    if not session.get('user_id'):
        from flask import redirect, url_for
        return redirect(url_for('auth.login'))

    user_id = session['user_id']
    _load_history(user_id)
    accessible_kbs = get_user_accessible_kbs(user_id)
    messages = _conversation_cache.get(user_id, [])
    return render_template('qa_index.html',
                           accessible_kbs=accessible_kbs,
                           username=session.get('username', ''),
                           chat_history=messages)


@bp.route('/qa/ask', methods=['POST'])
def ask():
    """Ask a question across all KBs — streaming response"""
    if not session.get('user_id'):
        logger.warn("[QA] ask | unauthorized access attempt")
        return jsonify({'error': '请先登录'}), 401

    data = request.json or {}
    session_id = data.get('session_id', '')
    clear = data.get('clear', False)
    query = data.get('query', '').strip()
    requested_kb_ids = data.get('kb_ids')  # None means all
    provider = data.get('provider')  # optional override

    user_id = session['user_id']

    # Clear history if requested (new conversation)
    if clear and session_id:
        _conversation_cache[user_id] = []
        from app.models import delete_user_history
        delete_user_history(user_id)
        logger.info(f"[QA] ask | history cleared for user_id={user_id}")
        return jsonify({'status': 'cleared'})

    if not query:
        return jsonify({'error': '问题不能为空'}), 400

    logger.info(f"[QA] ask | user_id={session['user_id']} | query='{query[:80]}' | session_id={session_id} | clear={clear}")

    # Ensure history is loaded
    _load_history(user_id)

    # Add question to conversation cache
    if user_id not in _conversation_cache:
        _conversation_cache[user_id] = []
    _conversation_cache[user_id].append({'role': 'user', 'content': query})

    role_names = get_user_roles(user_id)
    logger.info(f"[QA] ask | user roles={role_names}")

    if not role_names:
        logger.warn(f"[QA] ask | no roles for user_id={user_id}")
        return jsonify({'answer': '您暂未分配任何角色，无法访问知识库。', 'sources': []})

    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute('SELECT role_id FROM roles WHERE role_name IN (%s)' %
                  ','.join(['?'] * len(role_names)), role_names)
        role_ids = [row['role_id'] for row in c.fetchall()]

    perms = get_kb_permissions_for_roles(role_ids)
    all_kbs = get_all_kbs()
    accessible_kbs = [kb for kb in all_kbs if perms.get(kb['kb_id'], {}).get('can_access')]

    # Filter to selected KBs if specified
    if requested_kb_ids is not None:
        accessible_kbs = [kb for kb in accessible_kbs if kb['kb_id'] in requested_kb_ids]

    logger.info(f"[QA] ask | accessible_kbs={[kb['kb_name'] for kb in accessible_kbs]}")

    if not accessible_kbs:
        logger.warn(f"[QA] ask | no accessible KBs for user_id={user_id}")
        return jsonify({'answer': '当前角色暂无可访问的知识库。', 'sources': []})

    from app.services.dify import build_dify_service

    all_chunks = []
    all_sources = []

    for kb in accessible_kbs:
        try:
            logger.info(f"[QA] ask | retrieving from KB={kb['kb_name']} (id={kb['kb_id']})")
            dify = build_dify_service(kb)
            result = dify.retrieve(query, top_k=20, search_method='hybrid_search', reranking_enable=True)
            if 'error' not in result:
                for chunk in result.get('results', []):
                    chunk['kb_name'] = kb['kb_name'] if hasattr(kb, '__getitem__') else kb.get('kb_name')
                    chunk['kb_id'] = kb['kb_id'] if hasattr(kb, '__getitem__') else kb.get('kb_id')
                all_chunks.extend(result.get('results', []))
                if result.get('results'):
                    all_sources.append({'kb_id': kb['kb_id'], 'kb_name': kb['kb_name']})
                logger.info(f"[QA] ask | KB={kb['kb_name']} got {len(result.get('results',[]))} chunks")
            else:
                logger.error(f"[QA] ask | KB={kb['kb_name']} retrieve error: {result['error']}")
        except Exception as e:
            logger.error(f"[QA] ask | KB={kb['kb_name']} exception: {e}")

    if not all_chunks:
        logger.warn(f"[QA] ask | no chunks retrieved for query='{query[:80]}'")
        def _generate_empty():
            yield f"data: {json.dumps({'answer': '抱歉，未在任何知识库中找到相关内容。', 'sources': [], 'chunks': []})}\n\n"
            yield f"data: {json.dumps({'done': True})}\n\n"
        return Response(_generate_empty(), mimetype='text/event-stream',
                        headers={'X-Accel-Buffering': 'no', 'Cache-Control': 'no-cache'})

    all_chunks.sort(key=lambda x: x.get('score', 0), reverse=True)
    top_chunks = all_chunks[:8]
    chunk_texts = [c['content'] for c in top_chunks]
    logger.info(f"[QA] ask | total chunks={len(all_chunks)}, using top {len(top_chunks)} for LLM")

    # Stream the answer as SSE
    @stream_with_context
    def generate():
        # Immediately flush a "processing" token so browser gets data ASAP
        yield f"data: {json.dumps({'token': '正在检索知识库并生成回答...'})}\n\n"
        from app.services.llm import generate_answer_stream
        answer_text = ''
        for event in generate_answer_stream(chunk_texts, query, provider=provider):
            if event['type'] == 'error':
                logger.error(f"[QA] ask | LLM error: {event['content']}")
                yield f"data: {json.dumps({'error': event['content']})}\n\n"
                return
            text = event['content']
            answer_text += text
            yield f"data: {json.dumps({'token': text})}\n\n"

        logger.info(f"[QA] ask | answer complete, length={len(answer_text)}")
        # Save to DB
        save_query_log(user_id, accessible_kbs[0]['kb_id'], query, answer_text, 0)
        # Add to conversation cache
        _conversation_cache[user_id].append({'role': 'assistant', 'content': answer_text})
        yield f"data: {json.dumps({'done': True, 'sources': all_sources})}\n\n"

    return Response(
        generate(),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
            'Connection': 'keep-alive',
        }
    )


@bp.route('/qa/<int:kb_id>', methods=['GET', 'POST'])
def chat(kb_id):
    if not session.get('user_id'):
        from flask import redirect, url_for
        return redirect(url_for('auth.login'))

    from app.models import get_kb_by_id, get_user_roles, get_kb_permissions_for_roles, get_db_conn

    kb = get_kb_by_id(kb_id)
    if not kb:
        logger.warn(f"[QA] chat | KB not found: kb_id={kb_id}")
        return '知识库不存在', 404

    role_names = get_user_roles(session['user_id'])
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute('SELECT role_id FROM roles WHERE role_name IN (%s)' %
                  ','.join(['?'] * len(role_names)), role_names)
        role_ids = [row['role_id'] for row in c.fetchall()]

    perms = get_kb_permissions_for_roles(role_ids)
    perm = perms.get(kb_id, {})
    if not perm.get('can_access'):
        return '无权限访问该知识库', 403

    if request.method == 'POST':
        query = request.json.get('query', '').strip()
        provider = request.json.get('provider')
        if not query:
            return jsonify({'error': '问题不能为空'}), 400

        from app.services.dify import build_dify_service
        from app.services.llm import generate_answer
        from app.models import save_query_log

        dify = build_dify_service(kb)
        retrieve_result = dify.retrieve(query, top_k=5)

        if 'error' in retrieve_result:
            return jsonify({'error': retrieve_result['error']}), 500

        chunks = [r['content'] for r in retrieve_result.get('results', [])]

        if not chunks:
            answer = '抱歉，未在知识库中找到相关内容。'
            save_query_log(session['user_id'], kb_id, query, answer, 0)
            return jsonify({'answer': answer, 'chunks': []})

        answer, _ = generate_answer(chunks, query, provider=provider)
        save_query_log(session['user_id'], kb_id, query, answer, 0)
        return jsonify({'answer': answer, 'chunks': chunks})

    return render_template('qa_chat.html', kb=kb, can_access=bool(perm.get('can_access')))


@bp.route('/qa/<int:kb_id>/history')
def history(kb_id):
    if not session.get('user_id'):
        from flask import redirect, url_for
        return redirect(url_for('auth.login'))

    from app.models import get_user_history, get_kb_by_id
    history = get_user_history(session['user_id'], limit=50)
    kb = get_kb_by_id(kb_id)
    return render_template('qa_history.html', history=history, kb=kb)


@bp.route('/qa/<int:kb_id>/documents', methods=['GET'])
def kb_documents(kb_id):
    """获取知识库包含的文档列表"""
    if not session.get('user_id'):
        return jsonify({'error': '请先登录'}), 401

    from app.models import get_kb_by_id, get_user_roles, get_kb_permissions_for_roles, get_db_conn

    kb = get_kb_by_id(kb_id)
    if not kb:
        return jsonify({'error': '知识库不存在'}), 404

    role_names = get_user_roles(session['user_id'])
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute('SELECT role_id FROM roles WHERE role_name IN (%s)' %
                  ','.join(['?'] * len(role_names)), role_names)
        role_ids = [row['role_id'] for row in c.fetchall()]

    perms = get_kb_permissions_for_roles(role_ids)
    if not perms.get(kb_id, {}).get('can_access'):
        return jsonify({'error': '无权限'}), 403

    if not kb.get('dify_dataset_id'):
        return jsonify({'documents': [], 'total': 0})

    from app.services.dify import build_dify_service
    dify = build_dify_service(kb)
    result = dify.list_documents()
    return jsonify(result)
