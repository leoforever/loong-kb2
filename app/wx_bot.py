"""
loong-kb2 内嵌微信 Bot（per-user polling 架构）。

架构说明：
  - WxBotManager：全局管理器（纯 asyncio），通过 gevent.spawn 启动独立 greenlet
  - UserBot：每个用户绑定有独立的 _poll_loop 协程，共用同一个 ClientSession
  - 发消息时用该用户自己的 user_token，不再覆盖全局 token
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import secrets
import struct
import time
from typing import Callable, Optional

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

# ── Session 刷新 ───────────────────────────────────────────────────────────
async def _refresh_session(
    session: aiohttp.ClientSession,
    *,
    token: str,
    timeout_ms: int,
) -> dict:
    """调用 getconfig 刷新临时 session，返回新 sync_buf"""
    try:
        return await asyncio.wait_for(
            _api_post(session, endpoint=EP_GET_CONFIG,
                      payload={},
                      token=token, timeout_ms=timeout_ms),
            timeout=timeout_ms / 1000,
        )
    except asyncio.TimeoutError:
        return {"ret": -1, "errcode": -1, "errmsg": "timeout"}


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
    if ret != RATE_LIMIT_ERRCODE and errcode != RATE_LIMIT_ERRCODE:
        return False
    return (errmsg or "").lower() == "unknown error"


# ── iLink API ──────────────────────────────────────────────────────────────
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

    async with session.post(url, data=body.encode("utf-8"), headers=_headers(token, body)) as resp:
        raw = await resp.text()
        if not resp.ok:
            raise RuntimeError(f"iLink POST {endpoint} HTTP {resp.status}: {raw[:200]}")
        return json.loads(raw)


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

    async with session.get(url, headers=h) as resp:
        raw = await resp.text()
        if not resp.ok:
            raise RuntimeError(f"iLink GET {endpoint} HTTP {resp.status}: {raw[:200]}")
        return json.loads(raw)


async def _get_updates(
    session: aiohttp.ClientSession,
    *,
    token: str,
    sync_buf: str,
    timeout_ms: int,
) -> dict:
    try:
        return await asyncio.wait_for(
            _api_post(session, endpoint=EP_GET_UPDATES,
                      payload={"get_updates_buf": sync_buf},
                      token=token, timeout_ms=timeout_ms),
            timeout=timeout_ms / 1000,
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
    return await asyncio.wait_for(
        _api_post(session, endpoint=EP_SEND_MESSAGE, payload=payload,
                  token=token, timeout_ms=API_TIMEOUT_MS),
        timeout=API_TIMEOUT_MS / 1000,
    )


# ── 核心：调用本地 RAG ──────────────────────────────────────────────────────
def _call_rag(user_id: int, query: str) -> str:
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


# ════════════════════════════════════════════════════════════════════════════
# UserBot：每个用户绑定有独立的 polling 协程
# ════════════════════════════════════════════════════════════════════════════
class UserBot:
    """
    单个用户绑定的 polling 实例。
    每个 (openid, user_id, user_token) 三元组对应一个 UserBot，互不干扰。
    """

    def __init__(self, openid: str, user_id: int, user_token: str, session: aiohttp.ClientSession):
        self.openid = openid
        self.user_id = user_id
        self.user_token = user_token
        self._session = session
        self._running = False
        self._longpoll_timeout_ms = LONG_POLL_TIMEOUT_MS
        self._sync_buf = ""
        self._context_tokens: dict[str, str] = {}
        self._consecutive_failures = 0
        self._dedup: dict[str, float] = {}
        self._rate_limit_events: list[float] = []
        self._rate_limit_threshold = 1
        self._rate_limit_window = 30.0
        self._rate_limit_cooldown = 30.0

    # ── Polling ───────────────────────────────────────────────────────────────

    async def _poll_loop(self):
        self._running = True
        logger.info(f"[UserBot/{self.openid[:16]}] poll loop started, token={self.user_token[:24]}...")

        while self._running:
            try:
                result = await _get_updates(
                    self._session,
                    token=self.user_token,
                    sync_buf=self._sync_buf,
                    timeout_ms=self._longpoll_timeout_ms,
                )

                suggested = result.get("longpolling_timeout_ms")
                if isinstance(suggested, int) and suggested > 0:
                    self._longpoll_timeout_ms = suggested

                ret = result.get("ret", 0)
                errcode = result.get("errcode", 0)
                errmsg = result.get("errmsg", "")

                if (ret == SESSION_EXPIRED_ERRCODE or errcode == SESSION_EXPIRED_ERRCODE
                        or _is_stale_session_ret(ret, errcode, errmsg)):
                    logger.warning(f"[UserBot/{self.openid[:16]}] session expired (ret={ret} errcode={errcode}), refreshing session...")
                    # 调用 getconfig 刷新临时 session，不需要重新扫码
                    refresh = await _refresh_session(
                        self._session,
                        token=self.user_token,
                        timeout_ms=API_TIMEOUT_MS,
                    )
                    rf_ret = refresh.get("ret", -1)
                    rf_errcode = refresh.get("errcode", -1)
                    if rf_ret == 0 and rf_errcode == 0:
                        new_buf = refresh.get("get_updates_buf", "")
                        if new_buf:
                            self._sync_buf = new_buf
                            self._persist_sync_buf(new_buf)
                            logger.info(f"[UserBot/{self.openid[:16]}] session refreshed, new sync_buf={new_buf[:32]}...")
                        else:
                            logger.info(f"[UserBot/{self.openid[:16]}] session refreshed, no new sync_buf")
                    else:
                        logger.error(f"[UserBot/{self.openid[:16]}] session refresh failed: ret={rf_ret} errcode={rf_errcode} errmsg={refresh.get('errmsg')!r}")
                    await asyncio.sleep(3)  # 刷新失败则短等待后重试
                    self._consecutive_failures = 0
                    if self._context_tokens:
                        self._context_tokens = {}
                        self._persist_context_tokens()
                    continue

                if ret not in {0, None} or errcode not in {0, None}:
                    self._consecutive_failures += 1
                    if self._consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                        logger.warning(f"[UserBot/{self.openid[:16]}] {self._consecutive_failures} failures, backoff {BACKOFF_DELAY_SECONDS}s")
                        await asyncio.sleep(BACKOFF_DELAY_SECONDS)
                        self._consecutive_failures = 0
                    else:
                        await asyncio.sleep(RETRY_DELAY_SECONDS)
                    continue

                self._consecutive_failures = 0
                new_sync_buf = result.get("get_updates_buf", "")
                if new_sync_buf:
                    self._sync_buf = new_sync_buf
                    self._persist_sync_buf(new_sync_buf)

                for msg in result.get("msgs", []):
                    asyncio.create_task(self._process_message_safe(msg))

            except asyncio.TimeoutError:
                pass
            except Exception as exc:
                self._consecutive_failures += 1
                logger.error(f"[UserBot/{self.openid[:16]}] poll error ({self._consecutive_failures}/{MAX_CONSECUTIVE_FAILURES}): {exc}")
                if self._consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    await asyncio.sleep(BACKOFF_DELAY_SECONDS)
                    self._consecutive_failures = 0
                else:
                    await asyncio.sleep(RETRY_DELAY_SECONDS)

        logger.info(f"[UserBot/{self.openid[:8]}] poll loop stopped")

    # ── 消息处理 ─────────────────────────────────────────────────────────────

    async def _process_message_safe(self, msg: dict):
        try:
            msg_id = str(msg.get("message_id", ""))
            if msg_id:
                now = time.time()
                expired = [k for k, t in self._dedup.items() if now - t > MESSAGE_DEDUP_TTL_SECONDS]
                for k in expired:
                    del self._dedup[k]
                if msg_id in self._dedup:
                    return
                self._dedup[msg_id] = now
            await self._process_message(msg)
        except Exception as exc:
            logger.error(f"[UserBot/{self.openid[:8]}] unhandled inbound error: {exc}")

    async def _process_message(self, msg: dict):
        from_user_id = str(msg.get("from_user_id", "")).strip()
        room_id = str(msg.get("room_id", "") or msg.get("chat_room_id", "")).strip()
        peer_id = room_id or from_user_id
        if not peer_id:
            return
        if from_user_id == self.user_token:
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
        if not self._session:
            return
        logger.info(f"[UserBot/{self.openid[:8]}] RAG query text='{text[:60]}'")
        loop = asyncio.get_event_loop()
        answer = await loop.run_in_executor(None, _call_rag, self.user_id, text)
        await self._send_text_with_retry(peer_id, answer, context_token, client_id)
        logger.info(f"[UserBot/{self.openid[:8]}] sent reply len={len(answer)}")

    async def _send_text_with_retry(
        self,
        peer_id: str,
        text: str,
        context_token: str,
        client_id: str,
    ):
        if not self._session:
            return
        last_error: Optional[Exception] = None
        retried_without_token = False
        current_context_token = context_token or self._context_tokens.get(peer_id, "")

        for attempt in range(SEND_CHUNK_RETRIES + 1):
            if self._rate_limit_cooldown_remaining() > 0:
                logger.warning(f"[UserBot/{self.openid[:8]}] rate limit circuit open, dropping send")
                return

            try:
                resp = await _send_message(
                    self._session,
                    token=self.user_token,
                    to=peer_id,
                    text=text,
                    context_token=current_context_token,
                    client_id=client_id,
                )

                ret = resp.get("ret")
                errcode = resp.get("errcode")
                if ret not in {0, None} or errcode not in {0, None}:
                    is_session_expired = (
                        ret == SESSION_EXPIRED_ERRCODE
                        or errcode == SESSION_EXPIRED_ERRCODE
                        or _is_stale_session_ret(ret, errcode, resp.get("errmsg"))
                    )
                    if is_session_expired and not retried_without_token and current_context_token:
                        retried_without_token = True
                        current_context_token = ""
                        logger.warning(f"[UserBot/{self.openid[:8]}] session expired, retrying without context_token")
                        await asyncio.sleep(SEND_CHUNK_RETRY_DELAY)
                        continue

                    is_rate_limited = ret == RATE_LIMIT_ERRCODE or errcode == RATE_LIMIT_ERRCODE
                    if is_rate_limited:
                        self._record_rate_limit_event()
                        if self._rate_limit_cooldown_remaining() > 0:
                            return
                        await asyncio.sleep(SEND_CHUNK_RETRY_DELAY)
                        continue

                    last_error = RuntimeError(f"iLink send error: ret={ret} errcode={errcode}")
                    await asyncio.sleep(SEND_CHUNK_RETRY_DELAY)
                    continue
                return

            except Exception as exc:
                last_error = exc
                logger.warning(f"[UserBot/{self.openid[:8]}] send attempt {attempt+1} failed: {exc}")
                await asyncio.sleep(SEND_CHUNK_RETRY_DELAY)

        logger.error(f"[UserBot/{self.openid[:8]}] send exhausted retries: {last_error}")

    # ── 速率限制熔断器 ──────────────────────────────────────────────────────

    def _record_rate_limit_event(self):
        now = time.time()
        self._rate_limit_events.append(now)
        cutoff = now - self._rate_limit_window
        self._rate_limit_events = [t for t in self._rate_limit_events if t > cutoff]
        if len(self._rate_limit_events) >= self._rate_limit_threshold:
            self._rate_limit_circuit_until = now + self._rate_limit_cooldown
            self._rate_limit_events.clear()
            logger.warning(f"[UserBot/{self.openid[:8]}] rate limit circuit opened for {self._rate_limit_cooldown}s")

    def _rate_limit_cooldown_remaining(self) -> float:
        until = getattr(self, "_rate_limit_circuit_until", 0.0)
        remaining = until - time.time()
        return remaining if remaining > 0 else 0.0

    # ── 持久化 ───────────────────────────────────────────────────────────────

    def _persist_sync_buf(self, buf: str):
        try:
            from app.models import get_wx_bot_state
            state = get_wx_bot_state()
            sync_bufs: dict = state.get("sync_bufs", {}) or {}
            sync_bufs[self.openid] = buf
            from app.models import get_db_conn
            with get_db_conn() as conn:
                c = conn.cursor()
                c.execute(
                    "INSERT OR REPLACE INTO app_config (key, value) VALUES (?, ?)",
                    ("wx_bot_sync_bufs", json.dumps(sync_bufs, ensure_ascii=False))
                )
        except Exception as exc:
            logger.warning(f"[UserBot/{self.openid[:8]}] persist sync_buf failed: {exc}")

    def _persist_context_tokens(self, peer_id: Optional[str] = None, token: Optional[str] = None):
        try:
            from app.models import get_wx_bot_state
            state = get_wx_bot_state()
            ctx_map: dict = state.get("context_tokens", {}) or {}
            if peer_id and token:
                ctx_map[peer_id] = token
            elif self._context_tokens:
                ctx_map.update(self._context_tokens)
            from app.models import get_db_conn
            with get_db_conn() as conn:
                c = conn.cursor()
                c.execute(
                    "INSERT OR REPLACE INTO app_config (key, value) VALUES (?, ?)",
                    ("wx_bot_context_tokens", json.dumps(ctx_map, ensure_ascii=False))
                )
        except Exception as exc:
            logger.warning(f"[UserBot/{self.openid[:8]}] persist context_tokens failed: {exc}")


# ════════════════════════════════════════════════════════════════════════════
# WxBotManager：全局管理器（纯 asyncio，gevent greenlet 内运行）
# ════════════════════════════════════════════════════════════════════════════
class WxBotManager:
    """
    全局微信 Bot 管理器。

    通过 gevent.spawn 启动独立 greenlet，在其中运行 asyncio 主循环。
    持有共享 ClientSession，所有 UserBot polling 协程在同一个 loop 内并发运行。
    """

    def __init__(self, global_token: str = ""):
        self._global_token = global_token
        self._session: Optional[aiohttp.ClientSession] = None
        self._user_bots: dict[str, UserBot] = {}
        self._user_tasks: dict[str, asyncio.Task] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._running = False

    def start(self):
        """通过 gevent.spawn 在独立 greenlet 中启动（兼容 gunicorn gevent 模式）"""
        import gevent
        gevent.spawn(self._run_in_greenlet)
        logger.info("[WxBotManager] greenlet spawned")

    def _run_in_greenlet(self):
        """在 gevent greenlet 中运行 asyncio 主循环"""
        import sys
        sys.stderr.write("[WxBotManager] _run_in_greenlet ENTERED\n")
        sys.stderr.flush()
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        sys.stderr.write("[WxBotManager] event loop created\n")
        sys.stderr.flush()
        try:
            loop.run_until_complete(self._start_async())
            sys.stderr.write("[WxBotManager] _start_async complete, entering run_forever\n")
            sys.stderr.flush()
            loop.run_forever()  # 保持运行，直到 stop() 被调用
        except Exception as exc:
            sys.stderr.write(f"[WxBotManager] FATAL in greenlet: {exc}\n")
            sys.stderr.flush()
        finally:
            loop.close()
            sys.stderr.write("[WxBotManager] greenlet exiting\n")
            sys.stderr.flush()

    async def _start_async(self):
        """创建共享 session，从 DB 恢复所有活跃绑定"""
        self._session = aiohttp.ClientSession()
        logger.info("[WxBotManager] shared session created")

        try:
            from app.models import get_all_wechat_bindings, get_wx_bot_state
            bindings = get_all_wechat_bindings()
            state = get_wx_bot_state()
            sync_bufs: dict = state.get("sync_bufs", {}) or {}
            context_tokens: dict = state.get("context_tokens", {}) or {}

            for b in bindings:
                bb = dict(b)
                openid = bb.get("wechat_openid", "")
                user_id = bb.get("user_id")
                user_token = bb.get("user_token") or self._global_token
                is_active = bb.get("is_active")
                logger.info(f"[WxBotManager] DB row: openid={openid[:30]} user_id={user_id} is_active={is_active} user_token={user_token[:20] if user_token else 'EMPTY'}...")
                if not is_active:
                    continue
                if not openid or not user_id:
                    continue

                ub = UserBot(openid=openid, user_id=user_id,
                             user_token=user_token, session=self._session)
                ub._sync_buf = sync_bufs.get(openid, "")
                ub._context_tokens = {k: v for k, v in context_tokens.items()
                                      if k.startswith(openid)}

                self._user_bots[openid] = ub
                task = asyncio.create_task(self._run_user_bot(ub))
                self._user_tasks[openid] = task
                logger.info(f"[WxBotManager] restored UserBot openid={openid[:20]} user_id={user_id}")

            logger.info(f"[WxBotManager] restored {len(self._user_bots)} user bots")
        except Exception as e:
            logger.error(f"[WxBotManager] failed to restore user bots: {e}")

        self._running = True
        logger.info("[WxBotManager] async init complete")

    async def _run_user_bot(self, ub: UserBot):
        """运行单个 UserBot polling（捕获异常防止 task 崩溃）"""
        try:
            await ub._poll_loop()
        except asyncio.CancelledError:
            logger.info(f"[UserBot/{ub.openid[:8]}] task cancelled")
        except Exception as exc:
            logger.error(f"[UserBot/{ub.openid[:8]}] fatal error: {exc}")

    # ── 用户绑定/解绑 ───────────────────────────────────────────────────────

    def add_user(self, openid: str, user_id: int, user_token: str = ""):
        """添加新用户绑定，启动其 polling 协程"""
        token = user_token or self._global_token
        logger.info(f"[WxBotManager] add_user: openid={openid[:30]} user_id={user_id} token={token[:20]}...")

        if openid in self._user_bots:
            ub = self._user_bots[openid]
            old = ub.user_token
            ub.user_token = token
            logger.info(f"[WxBotManager] updated token for {openid[:20]}: {old[:20]}...")
            return

        if self._session is None:
            logger.warning(f"[WxBotManager] session not ready, UserBot queued: {openid[:30]}")
            return

        ub = UserBot(openid=openid, user_id=user_id, user_token=token, session=self._session)
        self._user_bots[openid] = ub

        if self._loop and self._running:
            task = asyncio.create_task(self._run_user_bot(ub))
            self._user_tasks[openid] = task
            logger.info(f"[WxBotManager] added UserBot openid={openid[:30]} user_id={user_id} [task created]")
        else:
            logger.warning(f"[WxBotManager] loop not ready, UserBot queued: {openid[:30]}")

    def remove_user(self, openid: str):
        """停止并移除指定 openid 的 UserBot"""
        task = self._user_tasks.pop(openid, None)
        if task:
            task.cancel()
            logger.info(f"[WxBotManager] cancelled task for openid={openid[:20]}")
        ub = self._user_bots.pop(openid, None)
        if ub:
            logger.info(f"[WxBotManager] removed UserBot openid={openid[:20]}")
        else:
            logger.warning(f"[WxBotManager] remove_user: openid={openid[:20]} not found")

    def get_token(self) -> str:
        return self._global_token

    def update_token(self, new_token: str):
        old = self._global_token
        self._global_token = new_token
        logger.info(f"[WxBotManager] Global token updated: {old[:8]}... -> {new_token[:8]}...")

    def stop(self):
        self._running = False
        for openid, task in list(self._user_tasks.items()):
            task.cancel()
        self._user_tasks.clear()
        self._user_bots.clear()
        if self._session:
            try:
                loop = asyncio.new_event_loop()
                loop.run_until_complete(self._session.close())
                loop.close()
            except Exception:
                pass
        if self._loop:
            self._loop.stop()
        logger.info("[WxBotManager] stopped")


# ════════════════════════════════════════════════════════════════════════════
# 回调注册 & 单例
# ════════════════════════════════════════════════════════════════════════════
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


_manager: Optional[WxBotManager] = None


def get_bot() -> Optional[WxBotManager]:
    return _manager


def start_wx_bot(ilink_token: str = ""):
    global _manager
    if _manager is None:
        _manager = WxBotManager(global_token=ilink_token)
        _manager.start()
        logger.info(f"[WxBot] WxBotManager started with global_token={ilink_token[:8]}...")
    return _manager


def stop_wx_bot():
    global _manager
    if _manager:
        _manager.stop()
        _manager = None
        logger.info("[WxBot] WxBotManager stopped")
