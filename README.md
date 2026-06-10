# 龙芯知识库 (Loong KB)

基于 Dify + Flask 的私有知识库问答系统，支持多用户权限管理和多知识库并行检索。

## 核心功能

- **多知识库管理**：支持从 Dify 同步知识库，每个知识库独立管理文档
- **RAG 问答**：多知识库并行检索 + LLM 汇总生成答案，支持流式输出
- **权限体系**：基于角色的权限控制，精细到每个知识库的访问/编辑/管理权限
- **多 LLM 支持**：通过配置切换 MiniMax / Qwen 等后端

## 技术栈

Flask · SQLite · Dify API · gevent

## 快速部署

### 1. 安装依赖

```bash
pip install flask gunicorn bcrypt requests pyyaml
```

### 2. 配置

```bash
cp config.yaml.example config.yaml
# 编辑 config.yaml，填入 Dify API 和 LLM 配置
```

### 3. 初始化并启动

```bash
python setup.py
./start.sh   # 生产环境
# 或
python run.py   # 开发调试
```

访问 `http://localhost:5001`，初始账号：`admin` / `admin123`

## 配置说明

### LLM（切换只需改一行）

```yaml
llm:
  provider: "qwen"          # minimax / qwen
  max_tokens: 2048

qwen:
  base_url: "http://YOUR_QWEN_SERVER:PORT"
  model: "Qwen3.5-27B-W8A8"

minimax:
  api_key: "YOUR_MINIMAX_KEY"
  base_url: "https://api.minimaxi.com/anthropic"
  model: "MiniMax-M2.7"
```

### Dify

```yaml
dify:
  api_url: "http://YOUR_DIFY_SERVER/v1"
  api_key: "YOUR_DIFY_KEY"
```