# 龙芯知识库 (Loong KB)

基于 Dify + Flask 的私有知识库问答系统，支持多用户权限管理和多知识库并行检索。

## 核心功能

- **多知识库管理**：支持从 Dify 同步知识库，每个知识库独立管理文档
- **RAG 问答**：多知识库并行检索 + LLM 汇总生成答案，支持流式输出
- **问答知识库**：纯本地 CSV 导入，无需 Dify，适合 FAQ、产品手册等固定问答场景
- **权限体系**：基于角色的权限控制，精细到每个知识库的访问/编辑/管理权限
- **多 LLM 支持**：通过配置切换 MiniMax / Qwen 等后端

## 知识库类型

系统支持 4 种知识库模板：

| 模板 | 说明 | 依赖 |
|------|------|------|
| 通用分段 | 自动分句，检索粒度均匀 | Dify |
| 父子分段-全文 | 整篇为父 chunk，子块 512 字，保留完整上下文 | Dify |
| 父子分段-段落 | 每个段落为父 chunk，子块 128 字，适合精确匹配 | Dify |
| 问答知识库 | 纯本地管理，CSV 导入问答对，不依赖 Dify | 无 |

## 技术栈

Flask · SQLite · FAISS · Dify API · gevent

## 快速部署

### 1. 安装依赖

```bash
pip install -r requirements.txt
# 核心依赖：flask gunicorn bcrypt requests pyyaml numpy faiss-cpu
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
python run.py   # 开发调试（代码变更自动 reload）
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

## 问答知识库使用指南

### 创建问答知识库

1. 以管理员身份登录，进入「知识库配置」
2. 点击「+ 添加知识库」
3. 选择「问答知识库」模板，填写名称和描述（无需填写 Dify 相关配置）
4. 点击创建

### 导入问答对

**方式一：CSV 批量导入**

上传 CSV 文件，格式如下：

```csv
问题,答案
龙芯3A5000支持多大内存?,最高支持 16GB DDR4
如何升级BMC固件?,通过 Web UI 进入 BMC 管理页面，选择固件升级选项
...
```

- 支持 `问题,答案` 或 `question,answer` 列名（中文优先）
- 首行可为表头，会自动跳过
- CSV 文件请使用 UTF-8 编码

**方式二：手动添加**

在知识库列表点击「问答管理」按钮，在弹窗中直接输入问题和答案进行添加。

### 检索流程

问答知识库的检索独立于 Dify 知识库，流程如下：

```
用户问题
    ↓
┌─────────────────────────────────────┐
│  并行检索所有可访问知识库              │
│                                     │
│  Dify 知识库：                       │
│    hybrid_search + reranking        │
│    top_k=20                         │
│                                     │
│  问答知识库：                         │
│    用户问题 → 嵌入向量               │
│    → FAISS 最近邻检索                │
│    → 返回匹配问答对 top 20           │
└─────────────────────────────────────┘
    ↓
合并 → 按相关度排序 → 取 top 8 chunks
    ↓
LLM 生成答案（流式返回）
```

### 索引重建

每次添加、删除、上传 CSV 后，FAISS 索引会自动更新。如需手动重建，点击「问答管理」→「重建索引」。

## 部署运维

```bash
./start.sh    # 启动服务
./stop.sh     # 停止服务
./deploy.sh   # 完整部署（停旧 → 安装依赖 → 初始化 → 启动）
python setup.py   # 初始化（新建管理员账号、同步 Dify 知识库）
```

## 目录结构

```
loong-kb/
├── app/
│   ├── routes/       # Flask 路由（qa.py, admin.py, auth.py）
│   ├── services/     # 业务逻辑（Dify、LLM、本地问答）
│   ├── models.py     # 数据库模型
│   └── templates/    # Jinja2 模板
├── cache/            # SQLite 数据库文件
├── local_qa_data/    # 本地问答知识库数据（FAISS 索引 + JSON 元数据）
├── config.yaml       # 配置文件
├── requirements.txt  # Python 依赖
├── setup.py         # 初始化脚本
└── *.sh             # 部署脚本
```
