# saltfisher

闲鱼（goofish）关键词/商品持续监控：持续采集、命中预算时实时推送、收藏商品降价追踪、
卖家上新盯梢，配上关键词行情分析（价格水位、降价排行、供应量趋势）和 LLM 单品建议，
外加一个操作台页面。单用户自托管，一个 Docker Compose 服务跑完全部功能。

## 文档

| 文档 | 内容 |
|---|---|
| [架构](docs/architecture.md) | 进程模型、三条采集路径与降级、11 张表、命中判定语义、通知去重、前端结构 |
| [运维手册](docs/operations.md) | 部署、配置项、凭证导入、备份恢复、数据增长、**故障 → 该做什么** |
| `/docs`（运行后） | 接口契约以 OpenAPI 为准，前端类型由它生成 |

## 三分钟跑起来

```bash
echo "SFD_API_TOKEN=$(openssl rand -hex 16)" >> .env
docker compose up -d
```

面板在 http://localhost:8000（只绑本机），用 `.env` 里的 token 登录。

然后**两件必做的事**，缺一件就等于没有监控：

1. 「设置」页导入采集凭证——没有它采集必被风控。取法见
   [运维手册的「唯一的人工前置」一节](docs/operations.md)，
   **不要按 cookie 名字找**
2. 「通知渠道」建一个渠道，并**在规则里勾上它**——空关联的规则不通知任何人

只想先看看界面：`uv run python scripts/seed_demo.py --reset`，从真实抓取的 fixture
生成演示数据，演示规则是停用状态，不碰上游。

## 技术栈

- 后端：Python 3.12 + FastAPI + SQLModel + SQLite(WAL)，由 **uv** 管理依赖
- 采集：mtop h5 接口优先，被风控或解析失败时降级 Playwright（**只有搜索有 DOM 兜底**，
  详情没有，原因见[架构文档的「采集」一节](docs/architecture.md)）
- 调度：进程内两条 asyncio 循环共用一个信号量（60s 下限 + 抖动）
- 前端：Vite + React 19 + TypeScript + TanStack Query + React Router，plain CSS 设计令牌
  （无组件库、无图表库、无 webfont）
- 分析：聚合统计量（价格水位/降价排行/供应量趋势）+ LLM 两个接入点（行情分析吃聚合量、
  单品建议吃单件全资料，可选带图）

## 开发环境

```bash
uv sync                                  # 安装依赖（含 dev 组）
git config core.hooksPath .githooks      # 启用 commit message 校验
git config commit.template .gitmessage   # 启用 commit 模板
```

后两条是本地配置，不随仓库分发，**每个 clone 都要执行一次**。

常用命令：

```bash
uv run ruff format .        # 格式化
uv run ruff check --fix .   # lint
uv run mypy app             # 类型检查
uv run pytest -q            # 测试
uv add <pkg>                # 加依赖（会同时更新 uv.lock）
```

## 通知渠道配置

渠道配置**不放 `.env`**，它是数据库里的一条记录，密钥存库、API 响应里脱敏。
面板「通知渠道」页可以直接建；要脚本化配置、以及邮件那两个必踩的 `535` 坑，见
[运维手册的「通知渠道的两条路」](docs/operations.md)。

**建好之后要在规则里勾上它**，否则命中不会推送给任何人。

## 备份与恢复

**不要用 `cp data/app.db` 备份运行中的实例**——WAL 模式下拷不到近期写入，会恢复出空库
（实测过：主库 114KB 旁边挂着 3.3MB 的 WAL，拷出来每张表 0 行）。

```bash
uv run python scripts/backup.py                    # 运行中也能用，会核对行数
uv run python scripts/backup.py --verify-only 路径  # 检查一份备份有没有数据
```

完整步骤与 `data/` 里哪些文件是敏感文件，见 [运维手册的「备份与恢复」](docs/operations.md)。

## Commit 规范

采用 Angular 规范（[参考](https://www.ruanyifeng.com/blog/2016/01/commit_message_change_log.html)）：

```
<type>(<scope>): <subject>

<body>

<footer>
```

- type：`feat` `fix` `docs` `style` `refactor` `test` `chore`
- scope：`collector` `notify` `watchlist` `api` `db` `scheduler` `analytics` `web` `spec` `docker` `deps`
- subject ≤ 50 字符，动词原形开头，小写，不加句号
- body 用 `- ` bullet 逐条列改动（changelog 风格），不写动机铺垫和总结句
- 不写 AI co-author 尾注

完整说明见 `.gitmessage`，由 `.githooks/commit-msg` 强制校验。改动校验规则后跑 `sh .githooks/commit-msg.test.sh`。
