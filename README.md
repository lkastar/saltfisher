# saltfish-digger

闲鱼（goofish）关键词/商品持续监控：定时采集、行情分析、管理页面，命中后通过邮件订阅或 Telegram Bot 推送。

单用户自托管，一个 Docker Compose 服务跑完全部功能。

## 技术栈

- 后端：Python 3.12 + FastAPI + SQLModel + SQLite(WAL)，由 **uv** 管理依赖
- 采集：mtop h5 接口优先，签名失效时降级 Playwright
- 调度：进程内 asyncio 轮询循环（60s 下限 + 抖动）
- 分析：pandas 跑价格快照历史
- 前端：Vite + React + TypeScript + TanStack Query

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

## Commit 规范

采用 Angular 规范（[参考](https://www.ruanyifeng.com/blog/2016/01/commit_message_change_log.html)）：

```
<type>(<scope>): <subject>

<body>

<footer>
```

- type：`feat` `fix` `docs` `style` `refactor` `test` `chore`
- scope：`collector` `notify` `api` `db` `scheduler` `analytics` `web` `spec` `docker` `deps`
- subject ≤ 50 字符，动词原形开头，小写，不加句号
- body 用 `- ` bullet 逐条列改动（changelog 风格），不写动机铺垫和总结句
- 不写 AI co-author 尾注

完整说明见 `.gitmessage`，由 `.githooks/commit-msg` 强制校验。改动校验规则后跑 `sh .githooks/commit-msg.test.sh`。
