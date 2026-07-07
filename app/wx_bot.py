"""
loong-kb2 内嵌微信 Bot（参考 iLink 协议实现）。

独立线程运行 async 事件循环：
  - 长轮询接收微信消息
  - 查用户绑定 openid→user_id
  - 调用本地 RAG（不经过外部 HTTP）
  - 通过 iLink API 发送回复
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import secrets
import struct
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

import aiohttp

logger = logging.getLogger(__name__)

# ── iLink 常量 ──────────────────────────────────────────────────────────────
ILINK_BASE_URL = "https://ilinkai.weixin.qq.com"
ILINK_APP_ID = "bot"
CHANNEL_VERSION = "2.2.0"
ILINK_APP_CLIENT_VERSION = (2 << 16) | (2 << 8) | 0

EP_GET_UPDATES = "ilink/bot/getupdates"
EP_SEND_MESSAGE = "ilink/bot/sendmessage"
EP_SEND_TYPING = "ilink/bot/sendtyping"
EP_GET_BOT_QR = "ilink/bot/get_bot_qrcode"
EP_GET_QR_STATUS = "ilink/bot/get_qrcode_status"

LONG_POLL_TIMEOUT_MS = 35_000
API_TIMEOUT_MS = 15_000
QR_TIMEOUT_MS = 35_000

ITEM_TEXT = 1
ITEM_IMAGE = 2
ITEM_VOICE = 3
ITEM_FILE = 4
ITEM_VIDEO = 5
MSG_TYPE_USER = 1
MSG_TYPE_BOT = 2
MSG_STATE_FINISH = 2
BOT_TYPE = 3
TYPING_START = 1
TYPING_STOP = 2


# ── AES 工具 ────────────────────────────────────────────────────────────────
def _pkcs7_pad(data: bytes, block_size: int = 16) -> bytes:
    pad_len = block_size - (len(data) % block_size)
    return data + bytes([pad_len] * pad_len)


def _aes128_ecb_encrypt(plaintext: bytes, key: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.backends import default_backend
    cipher = Cipher(algorithms.AES(key), modes.ECB(), backend=default_backend())
    encryptor = cipher.encryptor()
    return encryptor.update(_pkcs7_pad(plaintext)) + encryptor.finalize()


def _aes128_ecb_decrypt(ciphertext: bytes, key: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.backends import default_backend
    cipher = Cipher(algorithms.AES(key), modes.ECB(), backend=default_backend())
    decryptor = cipher.decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    if not padded:
        return padded
    pad_len = padded[-1]
    if 1 <= pad_len <= 16 and padded.endswith(bytes([pad_len]) * pad_len):
        return padded[:-pad_len]
    return padded


def _random_wechat_uin() -> str:
    value = struct.unpack(">I", secrets.token_bytes(4))[0]
    return base64.b64encode(str(value).encode("utf-8")).decode("ascii")


def _base_info() -> dict:
    return {"channel_version": CHANNEL_VERSION}


def _headers(token: Optional[str], body: str) -> dict:
    h = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "Content-Length": str(len(body.encode("utf-8"))),
        "X-WECHAT-UIN": _random_wechat_uin(),
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION),
    }
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


# ── iLink API ───────────────────────────────────────────────────────────────
async def _api_post(
    session: aiohttp.ClientSession,
    *,
    endpoint: str,
    payload: dict,
    token: Optional[str],
    timeout_ms: int,
) -> dict:
    body = json.dumps({**payload, "base_info": _base_info()}, ensure_ascii=False, separators=(",", ":"))
    url = f"{ILINK_BASE_URL.rstrip('/')}/{endpoint}"
    timeout = aiohttp.ClientTimeout(total=timeout_ms / 1000)
    async with session.post(url, data=body.encode("utf-8"), headers=_headers(token, body), timeout=timeout) as resp:
        raw = await resp.text()
        if not resp.ok:
            raise RuntimeError(f"iLink POST {endpoint} HTTP {resp.status}: {raw[:200]}")
        return json.loads(raw)


async def _api_get(
    session: aiohttp.ClientSession,
    *,
    endpoint: str,
    token: Optional[str],
    timeout_ms: int,
) -> dict:
    """iLink GET 请求（用于 QR 相关接口）"""
    url = f"{ILINK_BASE_URL.rstrip('/')}/{endpoint}"
    timeout = aiohttp.ClientTimeout(total=timeout_ms / 1000)
    h = {
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION),
    }
    if token:
        h["Authorization"] = f"Bearer {token}"
    async with session.get(url, headers=h, timeout=timeout) as resp:
        raw = await resp.text()
        if not resp.ok:
            raise RuntimeError(f"iLink GET {endpoint} HTTP {resp.status}: {raw[:200]}")
        return json.loads(raw)


async def _get_updates(session: aiohttp.ClientSession, *, token: str, sync_buf: str) -> dict:
    try:
        return await _api_post(
            session,
            endpoint=EP_GET_UPDATES,
            payload={"get_updates_buf": sync_buf},
            token=token,
            timeout_ms=LONG_POLL_TIMEOUT_MS,
        )
    except asyncio.TimeoutError:
        return {"ret": 0, "msgs": [], "get_updates_buf": sync_buf}


async def _send_message(session: aiohttp.ClientSession, *, token: str, to: str, text: str,
                        context_token: Optional[str], client_id: str) -> dict:
    """发送文本消息（参考 multi_channel_ai 的 msg wrapper 格式）"""
    payload = {
        "msg": {
            "from_user_id": "",
            "to_user_id": to,
            "client_id": client_id,
            "message_type": MSG_TYPE_BOT,
            "message_state": MSG_STATE_FINISH,
            "item_list": [{"type": ITEM_TEXT, "text_item": {"text": text}}],
        }
    }
    if context_token:
        payload["context_token"] = context_token
    return await _api_post(session, endpoint=EP_SEND_MESSAGE, payload=payload, token=token, timeout_ms=API_TIMEOUT_MS)


async def _get_bot_qr(session: aiohttp.ClientSession, *, token: str) -> dict:
    """获取二维码（GET + bot_type=3 参数）"""
    return await _api_get(
        session,
        endpoint=f"{EP_GET_BOT_QR}?bot_type={BOT_TYPE}",
        token=token,
        timeout_ms=API_TIMEOUT_MS,
    )


async def _get_qr_status(session: aiohttp.ClientSession, *, token: str, qr_id: str) -> dict:
    """查询二维码扫描状态"""
    return await _api_get(
        session,
        endpoint=f"{EP_GET_QR_STATUS}?qrcode={qr_id}&bot_type={BOT_TYPE}",
        token=token,
        timeout_ms=QR_TIMEOUT_MS,
    )


# ── 核心：调用本地 RAG ──────────────────────────────────────────────────────
def _call_rag(user_id: int, query: str) -> str:
    """
    直接调用本地 RAG（不走 HTTP），返回回答文本。
    """
    try:
        from app.routes.qa import (
            get_user_roles, get_kb_permissions_for_roles, get_all_kbs,
            _rerank_chunks, _clean_answer_reference,
        )
        from app.services.llm import generate_answer
        from app.services.rag_kb_service import RAGServerKBService
        from app.services.local_qa import search_local_qa

        role_names = get_user_roles(user_id)
        if not role_names:
            return "您暂未分配任何角色，无法访问知识库。"

        from app.models import get_db_conn
        with get_db_conn() as conn:
            c = conn.cursor()
            c.execute("SELECT role_id FROM roles WHERE role_name IN (%s)" %
                      ",".join(["?"] * len(role_names)), role_names)
            role_ids = [row["role_id"] for row in c.fetchall()]

        perms = get_kb_permissions_for_roles(role_ids)
        all_kbs = get_all_kbs()
        accessible_kbs = [kb for kb in all_kbs if perms.get(kb["kb_id"], {}).get("can_access")]

        if not accessible_kbs:
            return "当前角色暂无可访问的知识库。"

        all_chunks = []
        for kb in accessible_kbs:
            if kb.get("template_type") == "qa":
                try:
                    results = search_local_qa(kb["kb_id"], query, top_k=20)
                    for r in results:
                        all_chunks.append({
                            "content": f"问题：{r['question']}\n答案：{r['answer']}",
                            "score": r["score"],
                            "kb_name": kb["kb_name"],
                            "kb_id": kb["kb_id"],
                            "is_qa": True,
                        })
                except Exception as e:
                    logger.error(f"[WxBot] QA KB error: {e}")
            else:
                try:
                    svc = RAGServerKBService(rag_dataset_id=kb.get("rag_dataset_id", ""), kb_name=kb.get("kb_name", ""))
                    result = svc.retrieve(query, top_k=20, search_method="hybrid_search", reranking_enable=True)
                    if "error" not in result:
                        for chunk in result.get("results", []):
                            chunk["kb_name"] = kb["kb_name"]
                            chunk["kb_id"] = kb["kb_id"]
                            chunk["is_qa"] = False
                        all_chunks.extend(result.get("results", []))
                except Exception as e:
                    logger.error(f"[WxBot] RAG KB error: {e}")

        if not all_chunks:
            return "抱歉，未在任何知识库中找到相关内容。"

        all_chunks.sort(key=lambda x: x.get("score", 0), reverse=True)
        top_chunks = all_chunks[:8]
        top_chunks = _rerank_chunks(query, top_chunks)

        if not top_chunks:
            return "抱歉，未在任何知识库中找到相关内容。"

        chunk_texts = [c["content"] for c in top_chunks]
        answer, _ = generate_answer(chunk_texts, query)
        return _clean_answer_reference(answer)

    except Exception as e:
        logger.error(f"[WxBot] _call_rag error: {e}")
        return "知识库服务暂时不可用。"


# ── WxBot ───────────────────────────────────────────────────────────────────
class WxBot:
    """
    loong-kb2 内嵌微信 Bot，独立线程运行 async 事件循环。
    """

    def __init__(self, ilink_token: str):
        self._token = ilink_token
        self._session: Optional[aiohttp.ClientSession] = None
        self._sync_buf = ""
        self._running = False
        self._context_tokens: dict[str, str] = {}

    async def _poll(self):
        """长轮询接收消息"""
        if not self._session or not self._token:
            logger.warning("[WxBot] _poll: no session or token")
            await asyncio.sleep(5)
            return
        try:
            result = await _get_updates(self._session, token=self._token, sync_buf=self._sync_buf)

            # 检查 token 是否过期
            if result.get("errcode") == -14 or result.get("errmsg") == "session timeout":
                logger.warning("[WxBot] Bot token expired")
                if _on_token_expired_cb:
                    try:
                        if asyncio.iscoroutinefunction(_on_token_expired_cb):
                            await _on_token_expired_cb()
                        else:
                            _on_token_expired_cb()
                    except Exception as e:
                        logger.error(f"[WxBot] token_expired_cb error: {e}")
                await asyncio.sleep(10)
                return

            self._sync_buf = result.get("get_updates_buf", "")
            msgs = result.get("msgs", [])
            if msgs:
                logger.info(f"[WxBot] _poll: got {len(msgs)} msgs")
            for msg in msgs:
                await self._handle_msg(msg)
        except Exception as exc:
            logger.error(f"[WxBot] Poll error: {exc}")
            await asyncio.sleep(5)

    async def _handle_msg(self, msg: dict):
        """处理收到的微信消息"""
        try:
            # iLink 消息格式：from_user_id, room_id, chat_room_id, client_id, context_token
            from_user_id = msg.get("from_user_id", "")
            room_id = msg.get("room_id", "") or msg.get("chat_room_id", "")
            peer_id = room_id or from_user_id
            if not peer_id:
                return

            logger.info(f"[WxBot] recv msg | peer={peer_id[:20]} from={from_user_id[:20]} "
                        f"items={len(msg.get('item_list', []))}")

            context_token = msg.get("context_token", "")
            if context_token:
                self._context_tokens[peer_id] = context_token

            client_id = msg.get("client_id", "")
            items = msg.get("item_list", [])
            for item in items:
                if item.get("type") == ITEM_TEXT:
                    text = item.get("text_item", {}).get("text", "")
                    if text:
                        await self._reply(peer_id, text, context_token, client_id)
        except Exception as exc:
            logger.error(f"[WxBot] Handle msg error: {exc}")

    async def _reply(self, peer_id: str, text: str, context_token: str, client_id: str):
        """处理文本消息：查绑定→调用RAG→发回复"""
        try:
            # 1. 查 openid → user_id
            user_id = self._resolve_user(peer_id)
            if not user_id:
                logger.warn(f"[WxBot] unknown openid: {peer_id[:20]}")
                await self._send_text(peer_id, "未绑定账号，请先在网页端连接微信。", context_token, client_id)
                return

            logger.info(f"[WxBot] RAG query | user_id={user_id} text='{text[:60]}'")

            # 2. 同步调用本地 RAG（在 executor 里跑，不阻塞事件循环）
            loop = asyncio.get_event_loop()
            answer = await loop.run_in_executor(None, _call_rag, user_id, text)

            # 3. 发回复
            await self._send_text(peer_id, answer, context_token, client_id)
            logger.info(f"[WxBot] sent reply | len={len(answer)}")

        except Exception as exc:
            logger.error(f"[WxBot] Reply error: {exc}")

    def _resolve_user(self, openid: str) -> Optional[int]:
        """根据 openid 查找绑定的 user_id"""
        try:
            from app.models import get_db_conn
            with get_db_conn() as conn:
                c = conn.cursor()
                c.execute(
                    "SELECT user_id FROM wechat_bindings WHERE wechat_openid=? AND is_active=1",
                    (openid,)
                )
                row = c.fetchone()
                return row["user_id"] if row else None
        except Exception as e:
            logger.error(f"[WxBot] resolve_user error: {e}")
            return None

    async def _send_text(self, to: str, text: str, context_token: str, client_id: str):
        """通过 iLink 发送文本消息"""
        if not self._session:
            return
        try:
            await _send_message(
                self._session,
                token=self._token,
                to=to,
                text=text,
                context_token=context_token,
                client_id=client_id,
            )
        except Exception as exc:
            logger.error(f"[WxBot] Send error: {exc}")

    def update_token(self, new_token: str):
        """更新 bot token（扫码确认后由外部调用）"""
        old = self._token
        self._token = new_token
        logger.info(f"[WxBot] Token updated: {old[:8]}... -> {new_token[:8]}...")

    async def _run_loop(self):
        """事件循环"""
        self._session = aiohttp.ClientSession()
        self._running = True
        logger.info("[WxBot] Event loop started")
        while self._running:
            await self._poll()
        await self._session.close()
        logger.info("[WxBot] Event loop stopped")

    def start(self):
        """在新线程里启动 async 事件循环"""
        thread = threading.Thread(target=self._thread_target, daemon=True, name="WxBot")
        thread.start()
        logger.info("[WxBot] Thread started")

    def _thread_target(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._run_loop())
        finally:
            loop.close()

    def stop(self):
        self._running = False


# ── 回调注册 ────────────────────────────────────────────────────────────────
_on_token_expired_cb: Optional[Callable] = None
_on_user_bound_cb: Optional[Callable] = None
_on_user_unbound_cb: Optional[Callable] = None


def on_token_expired(cb: Callable):
    """Token 过期时调用，外部负责刷新 token"""
    global _on_token_expired_cb
    _on_token_expired_cb = cb


def on_user_bound(cb: Callable):
    """用户绑定回调: cb(openid, user_id)"""
    global _on_user_bound_cb
    _on_user_bound_cb = cb


def on_user_unbound(cb: Callable):
    """用户解绑回调: cb(openid)"""
    global _on_user_unbound_cb
    _on_user_unbound_cb = cb


# ── 单例 & 启动入口 ─────────────────────────────────────────────────────────
_bot: Optional[WxBot] = None


def get_bot() -> Optional[WxBot]:
    return _bot


def start_wx_bot(ilink_token: str):
    global _bot
    if _bot is None:
        _bot = WxBot(ilink_token)
        _bot.start()
    return _bot


def stop_wx_bot():
    global _bot
    if _bot:
        _bot.stop()
        _bot = None
