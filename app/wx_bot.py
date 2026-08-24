"""
loong-kb2 内嵌微信 Bot（参考 iLink 协议 + Hermes weixin.py 保活机制实现）。

独立线程运行 async 事件循环：
  - 长轮询接收微信消息（35s long-poll，心跳式保活）
  - 查用户绑定 openid→user_id
  - 调用本地 RAG（不经过外部 HTTP）
  - 通过 iLink API 发送回复（含 per-chunk retry + context_token 过期降级）
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
EP_GET_CONFIG = "ilink/bot/getconfig"

LONG_POLL_TIMEOUT_MS = 35_000
API_TIMEOUT_MS = 15_000
QR_TIMEOUT_MS = 35_000

# ── 错误处理常量 ────────────────────────────────────────────────────────────
MAX_CONSECUTIVE_FAILURES = 3
RETRY_DELAY_SECONDS = 2
BACKOFF_DELAY_SECONDS = 30
SESSION_EXPIRED_ERRCODE = -14
RATE_LIMIT_ERRCODE = -2
MESSAGE_DEDUP_TTL_SECONDS = 300
SEND_CHUNK_RETRIES = 4
SEND_CHUNK_RETRY_DELAY = 1.0

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


def _is_stale_session_ret(ret: Optional[int], errcode: Optional[int], errmsg: Optional[str]) -> bool:
    """ret=-2 / errcode=-2 + 'unknown error' 也是 stale session 信号，与 errcode=-14 等效。"""
    if ret != RATE_LIMIT_ERRCODE and errcode != RATE_LIMIT_ERRCODE:
        return False
    return (errmsg or "").lower() == "unknown error"


# ── iLink API（用 asyncio.wait_for 避免 ClientTimeout 在线程内的问题）─────────
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

    async def _do() -> dict:
        async with session.post(url, data=body.encode("utf-8"), headers=_headers(token, body)) as resp:
            raw = await resp.text()
            if not resp.ok:
                raise RuntimeError(f"iLink POST {endpoint} HTTP {resp.status}: {raw[:200]}")
            return json.loads(raw)

    return await asyncio.wait_for(_do(), timeout=timeout_ms / 1000)


async def _api_get(
    session: aiohttp.ClientSession,
    *,
    endpoint: str,
    token: Optional[str] = None,
    timeout_ms: int,
) -> dict:
    url = f"{ILINK_BASE_URL.rstrip('/')}/{endpoint}"
    h = {
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION),
    }
    if token:
        h["Authorization"] = f"Bearer {token}"

    async def _do() -> dict:
        async with session.get(url, headers=h) as resp:
            raw = await resp.text()
            if not resp.ok:
                raise RuntimeError(f"iLink GET {endpoint} HTTP {resp.status}: {raw[:200]}")
            return json.loads(raw)

    return await asyncio.wait_for(_do(), timeout=timeout_ms / 1000)


async def _get_updates(
    session: aiohttp.ClientSession,
    *,
    token: str,
    sync_buf: str,
    timeout_ms: int,
) -> dict:
    try:
        return await _api_post(
            session,
            endpoint=EP_GET_UPDATES,
            payload={"get_updates_buf": sync_buf},
            token=token,
            timeout_ms=timeout_ms,
        )
    except asyncio.TimeoutError:
        return {"ret": 0, "msgs": [], "get_updates_buf": sync_buf}


async def _send_message(
    session: aiohttp.ClientSession,
    *,
    token: str,
    to: str,
    text: str,
    context_token: Optional[str],
    client_id: str,
) -> dict:
    payload: dict = {
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
    return await _api_post(
        session, endpoint=EP_SEND_MESSAGE, payload=payload, token=token, timeout_ms=API_TIMEOUT_MS
    )


async def _get_bot_qr(session: aiohttp.ClientSession, *, token: str) -> dict:
    return await _api_get(
        session,
        endpoint=f"{EP_GET_BOT_QR}?bot_type={BOT_TYPE}",
        token=token,
        timeout_ms=API_TIMEOUT_MS,
    )


async def _get_qr_status(session: aiohttp.ClientSession, *, token: str, qr_id: str) -> dict:
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
            c.execute(
                "SELECT role_id FROM roles WHERE role_name IN (%s)" %
                ",".join(["?"] * len(role_names)), role_names
            )
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
                    svc = RAGServerKBService(
                        rag_dataset_id=kb.get("rag_dataset_id", ""),
                        kb_name=kb.get("kb_name", ""),
                    )
                    result = svc.retrieve(
                        query, top_k=20, search_method="hybrid_search", reranking_enable=True
                    )
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
    loong-kb2 内嵌微信 Bot。
    独立线程运行 async 事件循环，参考 Hermes weixin.py 的保活机制。
    """

    def __init__(self, ilink_token: str):
        self._token = ilink_token
        self._session: Optional[aiohttp.ClientSession] = None
        self._running = False
        self._longpoll_timeout_ms = LONG_POLL_TIMEOUT_MS

        # ── 持久化状态（从 DB 恢复）─────────────────────────────
        from app.models import get_wx_bot_state
        state = get_wx_bot_state()
        self._sync_buf: str = state.get("sync_buf", "")
        self._context_tokens: dict[str, str] = state.get("context_tokens", {})

        # ── 运行时状态 ──────────────────────────────────────────
        self._consecutive_failures = 0
        self._dedup: dict[str, float] = {}  # message_id → timestamp

        # ── 速率限制熔断器 ────────────────────────────────────
        self._rate_limit_events: list[float] = []
        self._rate_limit_threshold = 1  # 1次限流就熔断
        self._rate_limit_window = 30.0  # 30s 滑动窗口
        self._rate_limit_cooldown = 30.0  # 熔断 30s

    # ── Polling ─────────────────────────────────────────────────────────────

    async def _poll_loop(self):
        """主循环：35s long-poll + 分级重试 + 持久化游标"""
        self._session = aiohttp.ClientSession()
        self._running = True
        logger.info("[WxBot] Event loop started, sync_buf restored: len=%d", len(self._sync_buf))

        while self._running:
            try:
                result = await _get_updates(
                    self._session,
                    token=self._token,
                    sync_buf=self._sync_buf,
                    timeout_ms=self._longpoll_timeout_ms,
                )

                # 服务端建议的新超时，跟随它可以减少无效等待
                suggested = result.get("longpolling_timeout_ms")
                if isinstance(suggested, int) and suggested > 0:
                    self._longpoll_timeout_ms = suggested

                ret = result.get("ret", 0)
                errcode = result.get("errcode", 0)
                errmsg = result.get("errmsg", "")

                # ── Session 过期（bot token 失效）─────────────────
                if (ret == SESSION_EXPIRED_ERRCODE or errcode == SESSION_EXPIRED_ERRCODE
                        or _is_stale_session_ret(ret, errcode, errmsg)):
                    logger.error("[WxBot] Session expired (ret=%s errcode=%s); pausing 600s", ret, errcode)
                    await asyncio.sleep(600)
                    self._consecutive_failures = 0
                    # 清空 context_tokens，下一条消息走降级模式
                    if self._context_tokens:
                        self._context_tokens = {}
                        self._persist_context_tokens()
                    continue

                # ── 普通 API 错误 ──────────────────────────────────
                if ret not in {0, None} or errcode not in {0, None}:
                    self._consecutive_failures += 1
                    if self._consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                        logger.warning(
                            "[WxBot] %d consecutive failures, backing off %ds",
                            self._consecutive_failures, BACKOFF_DELAY_SECONDS,
                        )
                        await asyncio.sleep(BACKOFF_DELAY_SECONDS)
                        self._consecutive_failures = 0
                    else:
                        await asyncio.sleep(RETRY_DELAY_SECONDS)
                    continue

                # ── 成功 ──────────────────────────────────────────
                self._consecutive_failures = 0
                new_sync_buf = result.get("get_updates_buf", "")
                if new_sync_buf:
                    self._sync_buf = new_sync_buf
                    self._persist_sync_buf(new_sync_buf)

                # 并发分发消息（不阻塞后续 poll）
                for msg in result.get("msgs", []):
                    asyncio.create_task(self._process_message_safe(msg))

            except asyncio.TimeoutError:
                # longpoll 超时是正常行为，不算失败
                pass
            except Exception as exc:
                self._consecutive_failures += 1
                logger.error(
                    "[WxBot] poll error (%d/%d): %s",
                    self._consecutive_failures, MAX_CONSECUTIVE_FAILURES, exc,
                )
                if self._consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    await asyncio.sleep(BACKOFF_DELAY_SECONDS)
                    self._consecutive_failures = 0
                else:
                    await asyncio.sleep(RETRY_DELAY_SECONDS)

        await self._session.close()
        logger.info("[WxBot] Event loop stopped")

    # ── 消息处理 ─────────────────────────────────────────────────────────────

    async def _process_message_safe(self, msg: dict):
        """带去重的消息处理器"""
        try:
            msg_id = str(msg.get("message_id", ""))
            if msg_id:
                now = time.time()
                # 清理过期条目（5分钟滑动窗口）
                expired = [k for k, t in self._dedup.items() if now - t > MESSAGE_DEDUP_TTL_SECONDS]
                for k in expired:
                    del self._dedup[k]
                if msg_id in self._dedup:
                    logger.debug("[WxBot] dedup: skip duplicate msg_id=%s", msg_id[:20])
                    return
                self._dedup[msg_id] = now

            await self._process_message(msg)
        except Exception as exc:
            logger.error("[WxBot] unhandled inbound error from=%s: %s",
                        str(msg.get("from_user_id", ""))[:20], exc)

    async def _process_message(self, msg: dict):
        """处理单条微信消息"""
        from_user_id = str(msg.get("from_user_id", "")).strip()
        room_id = str(msg.get("room_id", "") or msg.get("chat_room_id", "")).strip()
        peer_id = room_id or from_user_id
        if not peer_id:
            return
        # 忽略自己发的消息
        if peer_id == self._token or peer_id == from_user_id and not room_id:
            pass
        if from_user_id == self._token:
            return

        context_token = str(msg.get("context_token", "")).strip()
        if context_token:
            self._context_tokens[peer_id] = context_token
            self._persist_context_tokens(peer_id, context_token)

        client_id = str(msg.get("client_id", ""))
        items = msg.get("item_list", [])
        for item in items:
            if item.get("type") == ITEM_TEXT:
                text = (item.get("text_item") or {}).get("text", "")
                if text:
                    await self._reply(peer_id, text, context_token, client_id)

    async def _reply(self, peer_id: str, text: str, context_token: str, client_id: str):
        """处理文本消息：查绑定→调用RAG→发送回复（含重试）"""
        user_id = self._resolve_user(peer_id)
        if not user_id:
            logger.warn(f"[WxBot] unknown openid: {peer_id[:20]}")
            await self._send_text_with_retry(
                peer_id, "未绑定账号，请先在网页端连接微信。",
                context_token, client_id,
            )
            return

        logger.info(f"[WxBot] RAG query | user_id={user_id} text='{text[:60]}'")

        loop = asyncio.get_event_loop()
        answer = await loop.run_in_executor(None, _call_rag, user_id, text)
        await self._send_text_with_retry(peer_id, answer, context_token, client_id)
        logger.info(f"[WxBot] sent reply | len={len(answer)}")

    # ── 发送（Hermes 风格：per-chunk retry + context_token 过期降级）──────────

    async def _send_text_with_retry(
        self,
        peer_id: str,
        text: str,
        context_token: str,
        client_id: str,
    ):
        """发送文本，带 per-chunk 重试和 context_token 降级。"""
        if not self._session:
            return

        last_error: Optional[Exception] = None
        retried_without_token = False
        current_context_token = context_token or self._context_tokens.get(peer_id, "")

        for attempt in range(SEND_CHUNK_RETRIES + 1):
            # 检查熔断器
            if self._rate_limit_cooldown_remaining() > 0:
                logger.warning("[WxBot] rate limit circuit open, dropping send to %s", peer_id[:20])
                return

            try:
                resp = await _send_message(
                    self._session,
                    token=self._token,
                    to=peer_id,
                    text=text,
                    context_token=current_context_token,
                    client_id=client_id,
                )

                ret = resp.get("ret")
                errcode = resp.get("errcode")
                if ret not in {0, None} or errcode not in {0, None}:
                    # ── Session 过期：去掉 context_token 再试一次 ──
                    is_session_expired = (
                        ret == SESSION_EXPIRED_ERRCODE
                        or errcode == SESSION_EXPIRED_ERRCODE
                        or _is_stale_session_ret(ret, errcode, resp.get("errmsg"))
                    )
                    if is_session_expired and not retried_without_token and current_context_token:
                        retried_without_token = True
                        current_context_token = ""
                        logger.warning("[WxBot] session expired for %s; retrying without context_token", peer_id[:20])
                        await asyncio.sleep(SEND_CHUNK_RETRY_DELAY)
                        continue

                    # ── 频率限制：记录事件，触发熔断 ────────────────
                    is_rate_limited = ret == RATE_LIMIT_ERRCODE or errcode == RATE_LIMIT_ERRCODE
                    if is_rate_limited:
                        self._record_rate_limit_event()
                        errmsg = resp.get("errmsg") or resp.get("msg") or "rate limited"
                        last_error = RuntimeError(f"iLink rate limited: ret={ret} errcode={errcode} errmsg={errmsg}")
                        if self._rate_limit_should_circuit():
                            logger.warning("[WxBot] rate limit circuit opened for %ds", self._rate_limit_cooldown)
                            return
                        await asyncio.sleep(SEND_CHUNK_RETRY_DELAY)
                        continue

                    # 其他错误
                    last_error = RuntimeError(f"iLink send error: ret={ret} errcode={errcode} errmsg={resp.get('errmsg')}")
                    await asyncio.sleep(SEND_CHUNK_RETRY_DELAY)
                    continue

                # 发送成功
                return

            except Exception as exc:
                last_error = exc
                logger.warning("[WxBot] send attempt %d failed: %s", attempt + 1, exc)
                await asyncio.sleep(SEND_CHUNK_RETRY_DELAY)

        # 所有重试都失败
        logger.error("[WxBot] send exhausted retries for %s: %s", peer_id[:20], last_error)

    # ── 速率限制熔断器 ───────────────────────────────────────────────────────

    def _record_rate_limit_event(self):
        now = time.time()
        self._rate_limit_events.append(now)
        # 只保留窗口内事件
        cutoff = now - self._rate_limit_window
        self._rate_limit_events = [t for t in self._rate_limit_events if t > cutoff]

    def _rate_limit_should_circuit(self) -> bool:
        """窗口内限流次数达到阈值则熔断"""
        if len(self._rate_limit_events) >= self._rate_limit_threshold:
            self._rate_limit_events.clear()
            return True
        return False

    def _rate_limit_cooldown_remaining(self) -> float:
        """熔断冷却剩余时间（秒）"""
        # 简化：记录第一次熔断时间
        if not hasattr(self, "_rate_limit_circuit_until"):
            return 0.0
        remaining = getattr(self, "_rate_limit_circuit_until", 0.0) - time.time()
        return remaining if remaining > 0 else 0.0

    # ── 持久化 ─────────────────────────────────────────────────────────────

    def _persist_sync_buf(self, buf: str):
        try:
            from app.models import set_wx_bot_state
            set_wx_bot_state(sync_buf=buf)
        except Exception as exc:
            logger.warning("[WxBot] persist sync_buf failed: %s", exc)

    def _persist_context_tokens(self, peer_id: str = None, token: str = None):
        """写 context_tokens 到 DB（只更新有值的字段）"""
        try:
            from app.models import set_wx_bot_state
            if peer_id and token:
                # 合并更新
                existing = dict(self._context_tokens)
                existing[peer_id] = token
                set_wx_bot_state(context_tokens=existing)
            elif self._context_tokens:
                set_wx_bot_state(context_tokens=self._context_tokens)
        except Exception as exc:
            logger.warning("[WxBot] persist context_tokens failed: %s", exc)

    # ── 辅助 ────────────────────────────────────────────────────────────────

    def _resolve_user(self, openid: str) -> Optional[int]:
        """根据 openid 查找绑定的 user_id"""
        try:
            from app.models import get_db_conn
            with get_db_conn() as conn:
                c = conn.cursor()
                c.execute(
                    "SELECT user_id FROM wechat_bindings WHERE wechat_openid=? AND is_active=1",
                    (openid,),
                )
                row = c.fetchone()
                return row["user_id"] if row else None
        except Exception as e:
            logger.error(f"[WxBot] resolve_user error: {e}")
            return None

    def update_token(self, new_token: str):
        """更新 bot token（扫码确认后由外部调用）"""
        old = self._token
        self._token = new_token
        logger.info(f"[WxBot] Token updated: {old[:8]}... -> {new_token[:8]}...")

    # ── 生命周期 ───────────────────────────────────────────────────────────

    def start(self):
        """在新线程里启动 async 事件循环"""
        thread = threading.Thread(target=self._thread_target, daemon=True, name="WxBot")
        thread.start()
        logger.info("[WxBot] Thread started")

    def _thread_target(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._poll_loop())
        finally:
            loop.close()

    def stop(self):
        self._running = False


# ── 回调注册 ───────────────────────────────────────────────────────────────
_on_token_expired_cb: Optional[Callable] = None
_on_user_bound_cb: Optional[Callable] = None
_on_user_unbound_cb: Optional[Callable] = None


def on_token_expired(cb: Callable):
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
