# loong-kb2 微信模块架构分析

## 一、整体架构图

```
┌─────────────────────────────────────────────────────────────────┐
│ gunicorn master (worker_class="gevent")                          │
│                                                                  │
│  Worker 进程                                                      │
│  └── gevent.greenlet  ← WxBotManager._run_in_greenlet()          │
│       └── asyncio.new_event_loop()  ← 单一事件循环               │
│            ├── Task: UserBot0._poll_loop()  (admin)              │
│            ├── Task: UserBot1._poll_loop()  (test1)             │
│            ├── Task: UserBot2._poll_loop()  (新绑定)            │
│            └── 共享 aiohttp.ClientSession (TCP 连接池复用)       │
└─────────────────────────────────────────────────────────────────┘
```

**为什么是 gevent + asyncio 嵌套？**

gunicorn 启动时 `worker_class = "gevent"`，gevent 会 patch `socket`、`time.sleep`、`threading._sleep`。如果直接 `threading.Thread(target=asyncio.run)`，`time.sleep()` 会被 gevent 的非阻塞版本替代，导致 `asyncio.sleep()` 永久卡住。解决方案：**在 gevent greenlet 内运行 asyncio 事件循环**，因为 greenlet 是协程切换而非线程，`asyncio.sleep()` 的等待不触发 gevent 的 blocking 检测。

---

## 二、iLink 凭证体系（三层分离）

```
全局 token（app_config.wx_bot_token）
  └─ 用于生成/查询二维码、管理操作
  └─ 由 admin 手动设置于 /admin/wxbot/token 页面

user_token（wechat_bindings.user_token，per-user）
  └─ 用户扫码授权时 iLink 返回，存 DB
  └─ 轮询和发消息时使用，代表"以哪个 ClawBot 身份操作"

sync_buf（app_config.wx_bot_sync_bufs[openid]）
  └─ iLink 返回的消息同步游标
  └─ 下次 get_updates 时携带，告诉服务端"我已经收到第 N 条消息"
```

**三层分离的意义**：
- admin 可以随时更换全局 token（换 ClawBot 账号）
- 但每个用户的 `user_token` 不会受影响（一个 ClawBot 可以绑定多个用户）
- 即使 `sync_buf` 丢失，调用 `getconfig` 刷新即可，不需要重新扫码

---

## 三、扫码绑定流程

### 步骤 1：前端请求二维码

```
GET /api/weixin/qr
  ↓
_get_bot_token()                              # wechat_bind.py:27-34
  → 先查 WxBotManager.get_token()
  → fallback 到 DB 的 wx_bot_token
  ↓
_ilink_get_bot_qr(token)                      # wechat_bind.py:81-93
  GET https://ilinkai.weixin.qq.com/ilink/bot/get_bot_qrcode?bot_type=3
  ↓
返回 qrcode(qr_id) + qrcode_img_content(base64 PNG)
```

注意：这里用的是 `EP_GET_BOT_QR`（GET 接口），不需要带 token。

### 步骤 2：前端轮询扫码状态

```
GET /api/weixin/binding-status/{qr_id}   # 前端每 2-3 秒轮询一次
  ↓
_ilink_get_qr_status(qr_id)             # wechat_bind.py:96-125
  GET https://ilinkai.weixin.qq.com/ilink/bot/get_qrcode_status?qrcode={qr_id}&bot_type=3
  ↓
状态流转: wait → scaned → confirmed
  confirmed 时 iLink 返回:
    ilink_bot_id   → 机器人的 openid
    bot_token      → user_token（关键！每个用户独立的凭证）
    ilink_user_id  → 微信用户的真实 openid
```

### 步骤 3：扫码确认，保存绑定

```python
# wechat_bind.py:174-193
if state == "confirmed":
    real_openid = user_openid or open_id       # 优先用微信用户真实 openid

    # ① 写入 DB（per-user token，不覆盖全局）
    upsert_wechat_binding(
        session['user_id'],     # user_id=1 (admin)
        real_openid,            # openid 完整格式（含 @im.wechat 后缀）
        user_token=token_from_qr  # iLink 返回的 bot_token
    )

    # ② 触发回调，启动该用户的 polling 协程
    _on_user_bound_cb(real_openid, session['user_id'], token_from_qr)
```

**关键**：`token_from_qr` 是 per-user 的，存到 `wechat_bindings.user_token`，不会写入全局 `wx_bot_token`。

---

## 四、`models.py` 数据层关键实现

### 4.1 `upsert_wechat_binding` — 绑定写入

```python
def upsert_wechat_binding(user_id, openid, user_token=None):
    """
    核心逻辑：
    1. 先查这个 openid 是否已被其他用户绑定
       → 被占用则 DELETE（旧用户解绑）
    2. 再查自己是否已绑定这个 openid
       → 已绑定则 UPDATE（更新 user_token 和时间戳）
       → 未绑定则 INSERT（新绑定）
    """
    with get_db_conn() as conn:
        c = conn.cursor()
        # ① openid 被其他用户占用？先删
        c.execute('SELECT user_id FROM wechat_bindings WHERE wechat_openid=? AND is_active=1', (openid,))
        row = c.fetchone()
        if row and row['user_id'] != user_id:
            c.execute('DELETE FROM wechat_bindings WHERE wechat_openid=?', (openid,))

        # ② 自己是否已绑定？
        c.execute(
            'SELECT id FROM wechat_bindings WHERE user_id=? AND wechat_openid=?',
            (user_id, openid)
        )
        existing = c.fetchone()
        if existing:
            # UPDATE（保留原记录，更新 token）
            c.execute(
                'UPDATE wechat_bindings SET is_active=1, created_at=CURRENT_TIMESTAMP, user_token=? WHERE id=?',
                (user_token, existing['id'])
            )
        else:
            # INSERT（新绑定）
            c.execute(
                'INSERT INTO wechat_bindings (user_id, wechat_openid, is_active, user_token) VALUES (?, ?, 1, ?)',
                (user_id, openid, user_token)
            )
```

**一个用户可以绑定多个微信**（`user_id` + `wechat_openid` 联合唯一），但一个 `wechat_openid` 只能被一个用户绑定。

### 4.2 `get_all_wechat_bindings` — 恢复时查询所有绑定

```python
def get_all_wechat_bindings():
    """启动时从 DB 恢复所有活跃绑定"""
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute('''
            SELECT wb.id, wb.user_id, wb.wechat_openid, wb.is_active, wb.created_at,
                   wb.user_token, u.username          ← 注意这里！必须包含 user_token
            FROM wechat_bindings wb
            JOIN users u ON wb.user_id = u.user_id
            ORDER BY wb.created_at DESC
        ''')
        return c.fetchall()      # 返回 sqlite3.Row，可迭代但没有 .get()
```

### 4.3 `get_wx_bot_state` — 恢复 polling 游标

```python
def get_wx_bot_state() -> dict:
    """读取WxBot持久化状态"""
    import json as _json
    with get_db_conn() as conn:
        c = conn.cursor()
        # sync_buf：per-user 的消息同步游标
        c.execute('SELECT value FROM app_config WHERE key=?', ('wx_bot_sync_bufs',))
        row = c.fetchone()
        sync_bufs = _json.loads(row['value']) if row and row['value'] else {}
        # sync_bufs 结构：{ "openid1": "buf1", "openid2": "buf2" }

        # context_token：每对话的上下文 token（用于多轮对话）
        c.execute('SELECT value FROM app_config WHERE key=?', ('wx_bot_context_tokens',))
        row = c.fetchone()
        context_tokens = _json.loads(row['value']) if row and row['value'] else {}
        # context_tokens 结构：{ "openid1:peer_id1": "ctx_token1", ... }

        return {'sync_bufs': sync_bufs, 'context_tokens': context_tokens}
```

---

## 五、`wx_bot.py` 核心实现

### 5.1 iLink API 请求封装

#### 请求头构造

```python
def _headers(token: Optional[str], body: str) -> dict:
    h = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",       # 固定值，表示"iLink bot 凭证认证"
        "Content-Length": str(len(body.encode("utf-8"))),
        "X-WECHAT-UIN": _random_wechat_uin(),         # 随机微信 UIN，防重放
        "iLink-App-Id": "bot",                        # 固定值
        "iLink-App-ClientVersion": str((2 << 16) | (2 << 8) | 0),  # 2.2.0
    }
    if token:
        h["Authorization"] = f"Bearer {token}"        # Bearer token 认证
    return h
```

#### POST 请求

```python
async def _api_post(session, endpoint, payload, token, timeout_ms):
    body = json.dumps({**payload, "base_info": _base_info()}, ...)
    url = f"{ILINK_BASE_URL}/{endpoint}"
    async with session.post(url, data=body.encode("utf-8"), headers=_headers(token, body)) as resp:
        raw = await resp.text()
        if not resp.ok:
            raise RuntimeError(f"HTTP {resp.status}: {raw[:200]}")
        return json.loads(raw)
```

#### get_updates（长轮询）

```python
async def _get_updates(session, token, sync_buf, timeout_ms):
    """
    iLink 长轮询接口。
    请求体: {"get_updates_buf": sync_buf, "base_info": {...}}
    正常返回: {"ret": 0, "errcode": 0, "msgs": [...], "get_updates_buf": new_buf}
    超时返回: {"ret": 0, "msgs": [], "get_updates_buf": sync_buf}（asyncio.wait_for 抛出 TimeoutError）
    session 过期: {"ret": -14, "errcode": -14, "errmsg": "session timeout"}
    """
    try:
        return await asyncio.wait_for(
            _api_post(session, endpoint=EP_GET_UPDATES,
                      payload={"get_updates_buf": sync_buf},
                      token=token, timeout_ms=timeout_ms),
            timeout=timeout_ms / 1000,     # asyncio.wait_for 控制 HTTP 超时
        )
    except asyncio.TimeoutError:
        return {"ret": 0, "msgs": [], "get_updates_buf": sync_buf}  # 超时不算错误
```

#### session 刷新

```python
async def _refresh_session(session, token, timeout_ms):
    """
    调用 getconfig 刷新临时 session，获取新的 sync_buf。
    用途：session 过期（errcode=-14）后，在不重新扫码的情况下恢复 polling。
    请求体: {"base_info": {...}}   ← 空 payload，只带 base_info
    返回: {"ret": 0, "errcode": 0, "get_updates_buf": new_buf}
    """
    try:
        return await asyncio.wait_for(
            _api_post(session, endpoint=EP_GET_CONFIG,
                      payload={},               # 空 payload，不需要额外参数
                      token=token, timeout_ms=timeout_ms),
            timeout=timeout_ms / 1000,
        )
    except asyncio.TimeoutError:
        return {"ret": -1, "errcode": -1, "errmsg": "timeout"}
```

### 5.2 `UserBot._poll_loop` — 核心轮询循环

```python
async def _poll_loop(self):
    self._running = True
    logger.info(f"[UserBot/{self.openid[:16]}] poll loop started, token={self.user_token[:24]}...")

    while self._running:
        try:
            # ① 调用 iLink get_updates（长轮询，35s）
            result = await _get_updates(
                self._session,
                token=self.user_token,           # ← per-user token，不混用
                sync_buf=self._sync_buf,         # ← 持久化的游标
                timeout_ms=self._longpoll_timeout_ms,
            )

            # ② 服务端建议调整轮询超时
            suggested = result.get("longpolling_timeout_ms")
            if isinstance(suggested, int) and suggested > 0:
                self._longpoll_timeout_ms = suggested

            ret = result.get("ret", 0)
            errcode = result.get("errcode", 0)

            # ③ Session 过期处理（核心修复点）
            if ret == SESSION_EXPIRED_ERRCODE or errcode == SESSION_EXPIRED_ERRCODE:
                logger.warning(f"[UserBot/{self.openid[:16]}] session expired, refreshing...")
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
                        self._persist_sync_buf(new_buf)  # 持久化新游标
                        logger.info(f"[UserBot/{self.openid[:16]}] session refreshed, new buf={new_buf[:32]}...")
                    else:
                        logger.info(f"[UserBot/{self.openid[:16]}] session refreshed")
                else:
                    logger.error(f"[UserBot/{self.openid[:16]}] refresh failed: ret={rf_ret} errcode={rf_errcode}")
                await asyncio.sleep(3)   # 刷新失败则短等待，不是 sleep 600s
                self._consecutive_failures = 0
                if self._context_tokens:
                    self._context_tokens = {}
                    self._persist_context_tokens()
                continue

            # ④ 其他错误处理（连续失败退避）
            if ret not in {0, None} or errcode not in {0, None}:
                self._consecutive_failures += 1
                if self._consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    logger.warning(f"[UserBot/{self.openid[:16]}] {self._consecutive_failures} failures, backoff {BACKOFF_DELAY_SECONDS}s")
                    await asyncio.sleep(BACKOFF_DELAY_SECONDS)  # 30s
                    self._consecutive_failures = 0
                else:
                    await asyncio.sleep(RETRY_DELAY_SECONDS)    # 2s
                continue

            # ⑤ 正常：重置失败计数，更新游标，处理消息
            self._consecutive_failures = 0
            new_sync_buf = result.get("get_updates_buf", "")
            if new_sync_buf:
                self._sync_buf = new_sync_buf
                self._persist_sync_buf(new_sync_buf)

            for msg in result.get("msgs", []):
                asyncio.create_task(self._process_message_safe(msg))  # 并发处理，不阻塞轮询

        except asyncio.TimeoutError:
            pass   # 长轮询正常超时，不需要处理
        except Exception as exc:
            self._consecutive_failures += 1
            logger.error(f"[UserBot/{self.openid[:16]}] poll error: {exc}")
            if self._consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                await asyncio.sleep(BACKOFF_DELAY_SECONDS)
                self._consecutive_failures = 0
            else:
                await asyncio.sleep(RETRY_DELAY_SECONDS)
```

**`sync_buf` 和游标机制**：每次 `get_updates` 返回新的 `get_updates_buf`，带上这个值下次再请求，iLink 就知道"你已经收到第 N 条消息了，我只返回第 N+1 条之后的新消息"，避免重复推送。

### 5.3 `UserBot._process_message` — 消息处理

```python
async def _process_message(self, msg: dict):
    from_user_id = str(msg.get("from_user_id", "")).strip()
    room_id = str(msg.get("room_id", "") or msg.get("chat_room_id", "")).strip()
    peer_id = room_id or from_user_id         # 群消息用 room_id，私聊用 from_user_id

    if not peer_id:
        return
    if from_user_id == self.user_token:       # 忽略自己发的消息（自己发给自己）
        return

    # 提取 context_token（多轮对话上下文）
    context_token = str(msg.get("context_token", "")).strip()
    if context_token:
        self._context_tokens[peer_id] = context_token
        self._persist_context_tokens(peer_id, context_token)

    # 遍历消息内容项（text/image/voice...）
    items = msg.get("item_list", [])
    for item in items:
        if item.get("type") == ITEM_TEXT:
            text = (item.get("text_item") or {}).get("text", "")
            if text:
                await self._reply(peer_id, text, context_token, client_id)
```

**为什么检查 `from_user_id == self.user_token`？** 因为发出去的消息也会通过同一个 `user_token` 回调回来，需要过滤掉自己发的消息避免无限循环。

### 5.4 `_call_rag` — 本地知识库检索

```python
def _call_rag(user_id: int, query: str) -> str:
    """
    1. 根据 user_id 查该用户的角色
    2. 根据角色查该用户有权限访问的知识库
    3. 对每个知识库执行检索（QA 库用 search_local_qa，文档库用 RAGServerKBService）
    4. 合并所有检索结果，重排序，取 top-8
    5. 调用 LLM 生成答案
    """
    role_names = get_user_roles(user_id)
    if not role_names:
        return "您暂未分配任何角色，无法访问知识库。"

    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT role_id FROM roles WHERE role_name IN (%s)" %
                  ",".join(["?"] * len(role_names)), role_names)
        role_ids = [row["role_id"] for row in c.fetchall()]

    perms = get_kb_permissions_for_roles(role_ids)
    all_kbs = get_all_kbs()
    accessible_kbs = [kb for kb in all_kbs if perms.get(kb["kb_id"], {}).get("can_access")]

    all_chunks = []
    for kb in accessible_kbs:
        if kb.get("template_type") == "qa":
            results = search_local_qa(kb["kb_id"], query, top_k=20)
            for r in results:
                all_chunks.append({
                    "content": f"问题：{r['question']}\n答案：{r['answer']}",
                    "score": r["score"],
                    "kb_name": kb["kb_name"],
                    "is_qa": True,
                })
        else:
            svc = RAGServerKBService(rag_dataset_id=kb.get("rag_dataset_id", ""))
            result = svc.retrieve(query, top_k=20, search_method="hybrid_search", reranking_enable=True)
            for chunk in result.get("results", []):
                chunk["kb_name"] = kb["kb_name"]
                chunk["is_qa"] = False
            all_chunks.extend(result.get("results", []))

    if not all_chunks:
        return "抱歉，未在任何知识库中找到相关内容。"

    all_chunks.sort(key=lambda x: x.get("score", 0), reverse=True)
    top_chunks = all_chunks[:8]
    top_chunks = _rerank_chunks(query, top_chunks)

    chunk_texts = [c["content"] for c in top_chunks]
    answer, _ = generate_answer(chunk_texts, query)
    return _clean_answer_reference(answer)
```

**为什么用 `run_in_executor`？** 因为 `_call_rag` 内部会调用同步的数据库查询和 HTTP 请求，asyncio 的事件循环在等待 I/O 时不能有同步阻塞操作（会卡住整个 loop）。用 `run_in_executor(None, ...)` 把这个同步函数扔到线程池执行，不阻塞 asyncio 事件循环。

### 5.5 `_send_text_with_retry` — 带重试的发消息

```python
async def _send_text_with_retry(self, peer_id, text, context_token, client_id):
    """
    发送流程（最多 4+1=5 次尝试）：
    1. 带 context_token 发送
    2. ret=-14（session 过期）→ 去掉 context_token 重试一次
    3. ret=-2（rate limit）→ 熔断器记录，30s 内不再发
    4. 其他错误 → 等 1s 重试
    5. 所有重试耗尽 → 记录错误日志
    """
    last_error: Optional[Exception] = None
    retried_without_token = False
    current_context_token = context_token or self._context_tokens.get(peer_id, "")

    for attempt in range(SEND_CHUNK_RETRIES + 1):   # 0..4，共 5 次
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
                # Session 过期：去掉 context_token 再试一次
                if (ret == SESSION_EXPIRED_ERRCODE or errcode == SESSION_EXPIRED_ERRCODE) \
                        and not retried_without_token and current_context_token:
                    retried_without_token = True
                    current_context_token = ""
                    logger.warning(f"[UserBot/{self.openid[:8]}] session expired, retrying without context_token")
                    await asyncio.sleep(SEND_CHUNK_RETRY_DELAY)
                    continue

                # Rate limit：熔断器记录
                if ret == RATE_LIMIT_ERRCODE or errcode == RATE_LIMIT_ERRCODE:
                    self._record_rate_limit_event()
                    if self._rate_limit_cooldown_remaining() > 0:
                        return
                    await asyncio.sleep(SEND_CHUNK_RETRY_DELAY)
                    continue

                last_error = RuntimeError(f"iLink send error: ret={ret} errcode={errcode}")
                await asyncio.sleep(SEND_CHUNK_RETRY_DELAY)
                continue
            return   # 发送成功，正常返回

        except Exception as exc:
            last_error = exc
            logger.warning(f"[UserBot/{self.openid[:8]}] send attempt {attempt+1} failed: {exc}")
            await asyncio.sleep(SEND_CHUNK_RETRY_DELAY)

    logger.error(f"[UserBot/{self.openid[:8]}] send exhausted retries: {last_error}")
```

---

## 六、`WxBotManager` — 全局管理器

### 6.1 `_run_in_greenlet` — gevent 嵌套 asyncio

```python
def _run_in_greenlet(self):
    """在 gevent greenlet 中运行 asyncio 主循环"""
    import sys
    sys.stderr.write("[WxBotManager] _run_in_greenlet ENTERED\n")
    sys.stderr.flush()
    loop = asyncio.new_event_loop()          # 创建新的事件循环
    asyncio.set_event_loop(loop)
    self._loop = loop
    sys.stderr.write("[WxBotManager] event loop created\n")
    sys.stderr.flush()
    try:
        loop.run_until_complete(self._start_async())   # 初始化（创建 session、恢复 bot）
        sys.stderr.write("[WxBotManager] _start_async complete, entering run_forever\n")
        sys.stderr.flush()
        loop.run_forever()                   # 永久运行，直到 stop() 被调用
    except Exception as exc:
        sys.stderr.write(f"[WxBotManager] FATAL in greenlet: {exc}\n")
        sys.stderr.flush()
    finally:
        loop.close()
        sys.stderr.write("[WxBotManager] greenlet exiting\n")
        sys.stderr.flush()
```

**为什么要 `run_until_complete` 再 `run_forever`？** `_start_async()` 里有 `await` 调用（协程），必须用 `run_until_complete` 执行完毕之后，才进入 `run_forever()` 让其他协程（如 `_poll_loop`）继续运行。

### 6.2 `_start_async` — 启动时从 DB 恢复所有绑定

```python
async def _start_async(self):
    """创建共享 session，从 DB 恢复所有活跃绑定"""
    self._session = aiohttp.ClientSession()   # 共享 TCP 连接池

    try:
        from app.models import get_all_wechat_bindings, get_wx_bot_state
        bindings = get_all_wechat_bindings()  # 查所有活跃绑定
        state = get_wx_bot_state()            # 查 sync_buf 和 context_tokens
        sync_bufs: dict = state.get("sync_bufs", {}) or {}
        context_tokens: dict = state.get("context_tokens", {}) or {}

        for b in bindings:
            bb = dict(b)                      # sqlite3.Row → dict（才有 .get()）
            openid = bb.get("wechat_openid", "")
            user_id = bb.get("user_id")
            user_token = bb.get("user_token") or self._global_token  # ← 关键修复！
            is_active = bb.get("is_active")
            logger.info(f"[WxBotManager] DB row: openid={openid[:30]} user_id={user_id} "
                        f"is_active={is_active} user_token={user_token[:20] if user_token else 'EMPTY'}...")

            if not is_active:
                continue
            if not openid or not user_id:
                continue

            ub = UserBot(openid=openid, user_id=user_id,
                         user_token=user_token, session=self._session)
            ub._sync_buf = sync_bufs.get(openid, "")   # 恢复该用户的游标
            ub._context_tokens = {k: v for k, v in context_tokens.items()
                                   if k.startswith(openid)}  # 恢复该用户的 context tokens

            self._user_bots[openid] = ub
            task = asyncio.create_task(self._run_user_bot(ub))  # 启动协程
            self._user_tasks[openid] = task
            logger.info(f"[WxBotManager] restored UserBot openid={openid[:20]} user_id={user_id}")

        logger.info(f"[WxBotManager] restored {len(self._user_bots)} user bots")
    except Exception as e:
        logger.error(f"[WxBotManager] failed to restore user bots: {e}")

    self._running = True
    logger.info("[WxBotManager] async init complete")
```

### 6.3 `add_user` — 动态添加新绑定

```python
def add_user(self, openid: str, user_id: int, user_token: str = ""):
    """
    用户扫码绑定时调用。创建该用户的 polling 协程。
    如果该 openid 已存在（重新扫码），则只更新 user_token，不重启协程。
    """
    token = user_token or self._global_token  # 有 token 优先用 per-user token
    logger.info(f"[WxBotManager] add_user: openid={openid[:30]} user_id={user_id} token={token[:20]}...")

    if openid in self._user_bots:
        # 已存在：只更新 token（用户重新扫码，token 变了）
        old = self._user_bots[openid].user_token
        self._user_bots[openid].user_token = token
        logger.info(f"[WxBotManager] updated token for {openid[:20]}: {old[:20]}...")
        return

    if self._session is None:
        # session 还没创建好（启动中），先排队
        logger.warning(f"[WxBotManager] session not ready, UserBot queued: {openid[:30]}")
        return

    # 创建新的 UserBot + 协程
    ub = UserBot(openid=openid, user_id=user_id, user_token=token, session=self._session)
    self._user_bots[openid] = ub

    if self._loop and self._running:
        task = asyncio.create_task(self._run_user_bot(ub))
        self._user_tasks[openid] = task
        logger.info(f"[WxBotManager] added UserBot openid={openid[:30]} user_id={user_id} [task created]")
    else:
        logger.warning(f"[WxBotManager] loop not ready, UserBot queued: {openid[:30]}")
```

### 6.4 `remove_user` — 解绑时停止协程

```python
def remove_user(self, openid: str):
    """解绑时取消该用户的 polling 协程"""
    task = self._user_tasks.pop(openid, None)
    if task:
        task.cancel()                          # asyncio.CancelledError，下一轮 poll 退出
        logger.info(f"[WxBotManager] cancelled task for openid={openid[:20]}")
    ub = self._user_bots.pop(openid, None)
    if ub:
        logger.info(f"[WxBotManager] removed UserBot openid={openid[:20]}")
    else:
        logger.warning(f"[WxBotManager] remove_user: openid={openid[:20]} not found")
```

---

## 七、`run.py` — 启动入口与回调链

```python
if token:
    from app.wx_bot import start_wx_bot, on_user_bound, on_user_unbound
    import app.wx_bot as _wb

    manager = start_wx_bot(token)             # 启动 WxBotManager
    logger.info("[WxBot] WxBotManager started")

    def _on_bind(openid, user_id, user_token):
        """用户扫码确认后，iLink 回调触发此函数"""
        logger.info(f"[WxBot] _on_bind called: openid={openid[:30]} user_id={user_id} "
                    f"token={user_token[:20]}...")
        manager.add_user(openid, user_id, user_token=user_token)  # ← 启动 polling 协程

    def _on_unbind(openid):
        """用户解绑时触发"""
        logger.info(f"[WxBot] _on_unbind called: openid={openid[:20]}")
        manager.remove_user(openid)

    _wb.on_user_bound(_on_bind)               # 注册绑定回调
    _wb.on_user_unbound(_on_unbind)           # 注册解绑回调
```

---

## 八、关键 Bug 修复记录

### Bug 1：`get_all_wechat_bindings` 漏查 `user_token` 列

**症状**：重启后两个 UserBot 的 `user_token` 都变成全局 token `d22dd2f4...`，各用户独立 token 未生效。

**根因**：SQL 查询 `SELECT ... u.username` 缺少 `wb.user_token`，导致恢复时 `bb.get("user_token")` 返回 `None` → fallback 到全局 token。

**修复**：
```sql
-- 修复前
SELECT wb.id, wb.user_id, wb.wechat_openid, wb.is_active, wb.created_at, u.username
-- 修复后
SELECT wb.id, wb.user_id, wb.wechat_openid, wb.is_active, wb.created_at,
       wb.user_token, u.username
```

### Bug 2：session 过期 sleep 600s

**症状**：进程重启后所有 UserBot 立即 `session expired, pausing 600s`，10 分钟内完全不轮询。

**根因**：旧代码把 session 过期当作永久断开处理，直接 `await asyncio.sleep(600)`。

**修复**：
```python
# 修复前
if errcode == SESSION_EXPIRED_ERRCODE:
    logger.error("session expired, pausing 600s")
    await asyncio.sleep(600)    # ← 错误：永久断开等待
    continue

# 修复后
if errcode == SESSION_EXPIRED_ERRCODE:
    refresh = await _refresh_session(session, token=self.user_token)
    if refresh["ret"] == 0:
        self._sync_buf = refresh["get_updates_buf"]
        self._persist_sync_buf(new_buf)
        # 继续 polling，不中断
    else:
        await asyncio.sleep(3)  # ← 失败则短暂重试，不是 600s
```

### Bug 3：gevent 环境下 threading.Thread 永久阻塞

**症状**：WxBotManager 启动后 polling 线程完全不运行。

**根因**：gunicorn gevent 模式 patch 了 `threading._sleep`，`time.sleep()` 被劫持成 gevent 的非阻塞版本，导致 `loop.run_until_complete()` 永远无法完成。

**修复**：用 `gevent.spawn()` 替代 `threading.Thread()`：
```python
# 修复前
import threading
self._thread = threading.Thread(target=self._run_in_thread, daemon=True)
self._thread.start()

def _run_in_thread(self):
    loop = asyncio.new_event_loop()
    loop.run_until_complete(self._start_async())  # ← gevent 下永久卡住
    loop.run_forever()

# 修复后
import gevent
gevent.spawn(self._run_in_greenlet)  # ← greenlet 内运行 asyncio，不受 gevent patch 影响
```

### Bug 4：`sqlite3.Row` 没有 `.get()` 方法

**症状**：代码 `bb.get("user_token")` 抛出 `AttributeError`。

**根因**：`sqlite3.Row` 支持下标访问 `row["col"]` 和迭代，但**没有 `.get()` 方法**。

**修复**：统一在取数据库行后立即转 dict：
```python
for b in bindings:
    bb = dict(b)          # ← sqlite3.Row → dict，才有 .get()
    user_token = bb.get("user_token") or self._global_token
```

---

## 九、持久化数据结构

```
app_config 表：
├── wx_bot_token                    → 全局 admin token（手动设置）
├── wx_bot_sync_bufs               → JSON: {openid: sync_buf, ...}
└── wx_bot_context_tokens          → JSON: {openid:peer_id: ctx_token, ...}

wechat_bindings 表：
├── id
├── user_id                        → 绑定到此微信的用户
├── wechat_openid                  → 微信 openid（含 @im.wechat 后缀）
├── is_active                      → 1=活跃，0=已解绑
└── user_token                     → 该用户绑定时的 iLink token（per-user）
```

`wx_bot_sync_bufs` 和 `wx_bot_context_tokens` 都是全局 key 下的 per-user 嵌套结构，通过 `openid` 作为二级索引区分不同用户。

---

## 十、错误码参考

| errcode | 含义 | 处理策略 |
|---------|------|---------|
| 0 | 正常 | 解析 msgs，更新 sync_buf |
| -14 | session 过期 | 调用 getconfig 刷新 session，失败则 sleep 3s 重试 |
| -2 | 频率限制 | 熔断器记录，30s 内不再发送 |
| 其他非0 | 接口错误 | 连续3次后退避 30s，否则重试间隔 2s |
