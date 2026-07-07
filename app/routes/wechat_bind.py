"""
WeChat binding routes.
用户扫码绑定自己的 openid → user_id，后续微信消息通过 wx_bot.py 接收并处理。

每个用户独立扫码绑定自己的微信账号，一个用户可绑定多个微信。

扫码流程（参考 multi_channel_ai）：
  1. GET  /api/weixin/qr          → iLink get_bot_qrcode（GET，无需 token）
  2. 前端轮询 GET /api/weixin/binding-status/{qr_id}
  3. confirmed 时写入 wechat_bindings 表，刷新 bot token
"""

from flask import Blueprint, jsonify, session, render_template, request
import logging, json, time, requests, io, base64, qrcode, secrets, struct

bp = Blueprint('wechat_bind', __name__)
logger = logging.getLogger(__name__)

# ── iLink 常量 ─────────────────────────────────────────────────────────────
ILINK_BASE_URL = "https://ilinkai.weixin.qq.com"
ILINK_APP_ID = "bot"
CHANNEL_VERSION = "2.2.0"
BOT_TYPE = 3
ILINK_APP_CLIENT_VERSION = str((2 << 16) | (2 << 8) | 0)


def _get_bot_token() -> str:
    """优先取运行时 bot token，其次从数据库读"""
    from app.wx_bot import get_bot
    bot = get_bot()
    if bot:
        return bot._token
    from app.models import get_app_config
    return get_app_config('wx_bot_token') or ''


# ── 页面路由 ───────────────────────────────────────────────────────────────
@bp.route('/bind/weixin')
def bind_page():
    """查看/管理已绑定的微信列表"""
    if 'user_id' not in session:
        from flask import redirect, url_for
        return redirect(url_for('auth.login'))
    from app.models import get_wechat_binding
    bindings = get_wechat_binding(session['user_id'])
    return render_template('wechat_bound.html', bindings=bindings,
                         openids=[b['wechat_openid'] for b in bindings])


@bp.route('/bind/weixin/add')
def bind_add_page():
    """追加新微信（强制显示二维码，不判断已有绑定）"""
    if 'user_id' not in session:
        from flask import redirect, url_for
        return redirect(url_for('auth.login'))
    return render_template('wechat_bind.html', add_mode=True)


def require_login():
    return 'user_id' in session


# ── iLink API（参考 weixin_adapter.py）────────────────────────────────────
def _ilink_headers(token: str = "") -> dict:
    """iLink 请求头"""
    value = struct.unpack(">I", secrets.token_bytes(4))[0]
    uin = base64.b64encode(str(value).encode("utf-8")).decode("ascii")
    h = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "X-WECHAT-UIN": uin,
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": ILINK_APP_CLIENT_VERSION,
    }
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _ilink_get_bot_qr(token: str) -> dict:
    """
    GET /ilink/bot/get_bot_qrcode
    参考 weixin_adapter.py: 这是 GET 接口，无需 token，返回 qrcode + qrcode_img_content
    """
    url = f"{ILINK_BASE_URL}/ilink/bot/get_bot_qrcode?bot_type={BOT_TYPE}"
    headers = {
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": ILINK_APP_CLIENT_VERSION,
    }
    resp = requests.get(url, headers=headers, timeout=10)
    resp.raise_for_status()
    return resp.json()


def _ilink_get_qr_status(qr_id: str, timeout: float = 35.0) -> dict:
    """
    GET /ilink/bot/get_qrcode_status
    参考 weixin_adapter.py: 这是 GET 接口，无 body，无 Authorization header
    iLink 返回: wait / scaned / confirmed
    confirmed 时额外字段: ilink_bot_id / bot_token / ilink_user_id
    """
    url = f"{ILINK_BASE_URL}/ilink/bot/get_qrcode_status?qrcode={qr_id}&bot_type={BOT_TYPE}"
    headers = {
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": ILINK_APP_CLIENT_VERSION,
    }
    resp = requests.get(url, headers=headers, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    raw_status = str(data.get("status", "wait"))
    if raw_status == "confirmed":
        return {
            "state": "confirmed",
            "open_id": data.get("ilink_bot_id", ""),
            "token": data.get("bot_token", ""),
            "user_id": data.get("ilink_user_id", ""),
            "raw": data,
        }
    elif raw_status == "scaned":
        return {"state": "scaned", "raw": data}
    elif raw_status == "expired":
        return {"state": "expired", "raw": data}
    else:
        return {"state": "pending", "raw": data}


# ── API 路由 ───────────────────────────────────────────────────────────────
@bp.route('/api/weixin/qr')
def get_qr():
    """生成二维码（GET /api/weixin/qr → 调用 iLink get_bot_qrcode）"""
    if not require_login():
        return jsonify({'error': '请先登录'}), 401

    try:
        token = _get_bot_token()
        data = _ilink_get_bot_qr(token)

        qr_url = data.get("qrcode_img_content", "")
        qr_id = data.get("qrcode", "")
        if not qr_id:
            raise Exception(f"iLink QR failed: ret={data.get('ret')} err={data.get('errmsg', data.get('err_msg'))}")

        # 生成二维码图片
        img = qrcode.make(qr_url)
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        img_b64 = base64.b64encode(buf.getvalue()).decode('utf-8')
        img_data_url = f"data:image/png;base64,{img_b64}"

        logger.info(f"[Wechat Bind] QR generated qr_id={qr_id[:20]} user_id={session['user_id']}")
        return jsonify({'qr_id': qr_id, 'qr_image': img_data_url})

    except requests.exceptions.Timeout:
        logger.error("[Wechat Bind] get_qr timeout")
        return jsonify({'error': '微信服务响应超时，请稍后重试'}), 500
    except Exception as e:
        logger.error(f"[Wechat Bind] get_qr error: {e}")
        return jsonify({'error': f'连接微信服务失败: {e}'}), 500


@bp.route('/api/weixin/binding-status/<qr_id>')
def binding_status(qr_id):
    """轮询扫码状态，被扫后保存绑定关系"""
    if not require_login():
        return jsonify({'error': '请先登录'}), 401

    try:
        result = _ilink_get_qr_status(qr_id, timeout=35.0)

        state = result.get("state", "pending")
        logger.info(f"[Wechat Bind] QR status: qr_id={qr_id[:20]} state={state}")

        if state == "confirmed":
            open_id = result.get("open_id", "")
            token_from_qr = result.get("token", "")
            user_openid = result.get("user_id", "")

            # 优先用 ilink_user_id（用户真实 openid），fallback 到 ilink_bot_id
            real_openid = user_openid or open_id

            if real_openid:
                from app.models import upsert_wechat_binding
                upsert_wechat_binding(session['user_id'], real_openid)
                logger.info(f"[Wechat Bind] Bound: user_id={session['user_id']} openid={real_openid[:20]}")

                from app.wx_bot import _on_user_bound_cb
                if _on_user_bound_cb:
                    try:
                        _on_user_bound_cb(real_openid, session['user_id'])
                    except Exception as e:
                        logger.error(f"[Wechat Bind] _on_user_bound_cb error: {e}")

                if token_from_qr:
                    from app.models import set_app_config
                    set_app_config('wx_bot_token', token_from_qr)
                    from app.wx_bot import get_bot
                    bot = get_bot()
                    if bot:
                        bot.update_token(token_from_qr)
                    logger.info(f"[Wechat Bind] Bot token updated from QR confirm")

                return jsonify({'status': 'bound', 'open_id': real_openid})
            else:
                logger.warning(f"[Wechat Bind] QR confirmed but no openid: {result.get('raw', result)}")
                return jsonify({'status': 'pending', 'error': 'confirmed but no openid'})

        elif state == "scaned":
            return jsonify({'status': 'scaned'})
        else:
            return jsonify({'status': state if state in ('expired', 'bound') else 'pending'})

    except requests.exceptions.Timeout:
        logger.warning(f"[Wechat Bind] QR status timeout qr_id={qr_id[:20]}, treating as pending")
        return jsonify({'status': 'pending'})
    except Exception as e:
        logger.error(f"[Wechat Bind] binding_status error: {e}")
        return jsonify({'error': str(e)}), 500


@bp.route('/api/weixin/status')
def binding_status_check():
    if not require_login():
        return jsonify({'error': '请先登录'}), 401
    from app.models import get_wechat_binding
    bindings = get_wechat_binding(session['user_id'])
    if bindings:
        return jsonify({'bound': True, 'count': len(bindings), 'openid': bindings[0]['wechat_openid']})
    return jsonify({'bound': False, 'count': 0})


@bp.route('/api/weixin/unbind', methods=['POST'])
def unbind():
    if not require_login():
        return jsonify({'error': '请先登录'}), 401
    openid_to_unbind = request.args.get('openid')
    from app.models import get_wechat_binding, unbind_wechat
    bindings = get_wechat_binding(session['user_id'])
    for b in bindings:
        if not openid_to_unbind or b['wechat_openid'] == openid_to_unbind:
            unbind_wechat(b['wechat_openid'])
            from app.wx_bot import _on_user_unbound_cb
            if _on_user_unbound_cb:
                try:
                    _on_user_unbound_cb(b['wechat_openid'])
                except Exception as e:
                    logger.error(f"[Wechat Bind] _on_user_unbound_cb error: {e}")
    return jsonify({'status': 'ok'})


@bp.route('/admin/wxbot/token', methods=['GET', 'POST'])
def admin_wxbot_token():
    """管理员查看/设置微信 Bot 的 iLink token"""
    from flask import g
    if not getattr(g, 'is_admin', False):
        return jsonify({'error': '需要管理员权限'}), 403

    if request.method == 'POST':
        token = (request.json or {}).get('token', '').strip()
        if not token:
            return jsonify({'error': 'token 不能为空'}), 400
        from app.models import set_app_config
        set_app_config('wx_bot_token', token)
        from app.wx_bot import get_bot
        bot = get_bot()
        if bot:
            bot.update_token(token)
        logger.info(f"[Wechat Bind] Admin updated bot token len={len(token)}")
        return jsonify({'status': 'ok', 'token_len': len(token)})

    current = _get_bot_token()
    masked = current[:4] + '***' + current[-4:] if current else ''
    return jsonify({'token': current, 'token_masked': masked})
