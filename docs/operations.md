# 运维手册

---

## 部署

```bash
docker compose up -d          # 首次会构建镜像
```

30 秒后 healthy，面板在 **http://localhost:8000**。只绑 `127.0.0.1`，远程访问走 SSH 隧道或
Tailscale——面板只有一个 Bearer token，不要直接暴露到公网。

```bash
docker compose logs -f
docker compose ps
docker compose down            # 停机时会 checkpoint WAL，留下自包含的 app.db
docker compose up -d --build   # 改了代码后重建
```

镜像基于 `mcr.microsoft.com/playwright/python`，但**浏览器是从 `uv.lock` 里那个 playwright
包装的**（`uv run playwright install chromium`），不是用镜像自带的。理由：镜像 tag 和锁文件
是同一个版本号的两个来源，实测漂移过一次，容器起不来。基础镜像只保留它的系统库价值。

### 配置项

只有 `SFD_API_TOKEN` 是必填，缺失时**拒绝启动**并说明原因。其余都有默认值：

| 变量 | 默认 | 说明 |
|---|---|---|
| `SFD_API_TOKEN` | — | **必填**，面板与 API 的 Bearer token，≥8 字符 |
| `SFD_DATA_DIR` | `data` | 数据库与 `state.json` 的位置 |
| `SFD_LOG_LEVEL` | `INFO` | 设 `DEBUG` 会非常吵，但不会泄露 cookie（传输层日志已压到 WARNING） |
| `SFD_MIN_INTERVAL_SECONDS` | `60` | **防封下限**，不是 UI 提示。前后端都镜像这个值 |
| `SFD_BROWSER_TIMEOUT_S` | `30` | |
| `SFD_SELLER_PROFILE_TTL_DAYS` | `7` | 卖家画像多久重抓 |
| `SFD_MAX_IMAGE_URLS` | `5` | 每个商品存几张图 |
| `SFD_PRICE_DROP_RATIO` | `0.05` | 降价要跌过多少才值得再推。整数分算术，不用浮点比例 |
| `SFD_RENOTIFY_COOLDOWN_MINUTES` | `30` | 区间边界反复横跳时的最小通知间隔 |
| `SFD_MAX_CONSECUTIVE_FAILURES` | `5` | 连续失败几次自动停用（风控挑战是**第 1 次**就停） |

从 `.env` 自动读取，`docker compose` 和 `uvicorn` 两条路都不需要导出变量。

---

## 唯一的人工前置：导入采集凭证

没有有效登录态 cookie，采集**必被风控**。容器里没法让人扫码或点滑块，所以这一步只能在你自己
的浏览器上做一次。

**不要按 cookie 名字找**，从你要复现的那个请求上复制：

1. 浏览器登录闲鱼，打开一个**商品详情页**（`goofish.com/item?id=…`）
2. 出现滑块就完成它，然后**刷新**
3. 开发者工具 → Network → 过滤 `detail` → 找到 **`mtop.taobao.idle.pc.detail`**
4. 点它 → Headers → Request Headers → 复制 `Cookie:` 那一整行
5. 面板「设置」页粘贴 → 导入

**为什么要指定这个请求**：它的 Cookie 头按定义就是详情端点实际收到的完整凭证集，域也一定对。
按名字找会漏掉设在别的域上、或名字与预期不同的凭证。（我们一度以为关键是 `x5sec`，那来自一份
研究笔记的**假设**；实测中用户过了滑块却没有它，而按请求复制之后它自己就出现了。）

**判定成功看行为，不看 cookie 名单**：设置页的「会话可用」只代表有 token；只有真实采集成功过
一次才会显示「最近一次采集成功于 …」。导入后去「监控任务」点一次「立即运行」即可确认。

凭证只存后端（内存 + `data/state.json`），API 与页面**永远只返回 cookie 名字**。

### 被风控之后

不需要盯面板：**采集一被挑战就会往所有启用的渠道发一条通知**，正文就是上面这五步。
（走所有启用渠道而不是规则关联的渠道——采集停的是全局，没关联渠道的规则否则会静默。
一次挑战只发一条，直到采集重新成功或重新导入凭证为止。）

上面那五步在代码里也有一份：`app/notify/base.py` 的 `CHALLENGE_STEPS`，改这里就一起改那里。

**重新导入后会自动做的事**：清掉挑战标记，并把**被风控停用的规则**重新启用（只有这一类；
手动停用的、和连续失败 5 次停用的都不动）。导入**不会**立刻触发采集——所有规则一起打上游正是
会被再次标记的那种请求量，交给正常轮询即可，或者自己点一次「立即运行」确认。

---

## 让通知真正发出去

两步，缺一步就等于没有通知：

1. 「通知渠道」页建一个邮件或 Telegram 渠道
2. **在「监控任务」的规则里勾上它**——空关联的规则不通知任何人，列表里会标 `无渠道`

（收藏夹降价走「所有启用渠道」，不需要关联。）

### 通知渠道的两条路

面板「通知渠道」页可以直接建。要脚本化配置就打 API——渠道配置**不放 `.env`**，
它是数据库里的一条记录，密钥存库、API 响应里脱敏。

#### 邮件（以 Brevo 为例，域名发信）

```bash
curl -X POST localhost:8000/api/channels \
  -H "Authorization: Bearer $SFD_API_TOKEN" -H 'content-type: application/json' \
  -d '{"kind":"email","label":"域名发信","config":{
        "smtp_host":"smtp-relay.brevo.com","smtp_port":587,
        "username":"<后台 SMTP & API 页的 SMTP login>",
        "password":"<同一页生成的 SMTP key，xsmtpsib-…>",
        "from_addr":"notify@你的域名",
        "to_addrs":["收件人@example.com"],
        "use_ssl":false,"use_starttls":true}}'
```

四个坑，前两个都表现为 `535 Authentication failed`：

- **`password` 填 SMTP key，不是 API key**——两者在后台同一页面但不通用
- **`username` 不要填 `smtp-relay.brevo.com`**——那是主机名
- 端口与加密要配对：587 走 STARTTLS（`use_ssl:false` + `use_starttls:true`）；465 则
  `use_ssl:true`
- 局域网内无认证中继：`password` 留空、两个开关都 `false`

#### Telegram

```bash
curl -X POST localhost:8000/api/channels \
  -H "Authorization: Bearer $SFD_API_TOKEN" -H 'content-type: application/json' \
  -d '{"kind":"telegram","label":"bot","config":{"bot_token":"123456:ABC…","chat_id":"…"}}'
```

自托管告警一般比邮件省事：没有域名认证和投递率问题。

#### 250 不等于送达

`/test` 返回 `ok` 只代表中继**接收**了。**发信域名必须在服务商那边认证过**
（SPF/DKIM/DMARC DNS 记录）；未认证时服务商仍会接收并回 250，但收件方会按 DMARC 拒收或
判垃圾。**第一次配好后必须去收件箱确认一次。**

---

## 备份与恢复

**不要用 `cp data/app.db` 备份运行中的实例。**

数据库跑在 WAL 模式，近期写入停在 `app.db-wal` 直到 checkpoint，而 SQLite 只在 WAL 超过
约 4MB 时自动 checkpoint。实测过：主库 114KB 旁边挂着 3.3MB 的 WAL，拷出来的 `app.db`
**每张表 0 行**——连 `CREATE TABLE` 都还在 WAL 里，拿到的是个没有任何表的空文件。
那样的备份恢复回去，会得到一个「恢复成功」的空库。

```bash
# 运行中备份（SQLite online backup API，无需停机，会核对源库与备份的逐表行数）
uv run python scripts/backup.py                     # → data/backups/app-<时间戳>.db
uv run python scripts/backup.py --out /mnt/nas/app.db

# 检查一份备份到底有没有数据
uv run python scripts/backup.py --verify-only /path/to/app.db

# 恢复
docker compose down                                 # 或停掉 uvicorn
rm -f data/app.db data/app.db-wal data/app.db-shm    # -wal/-shm 必须一起删
cp /path/to/backup.db data/app.db
docker compose up -d
```

干净停机之后 `cp data/app.db` 是安全的：进程退出时会 `PRAGMA wal_checkpoint(TRUNCATE)`
并释放连接。

### `data/` 目录里都是敏感文件

| 文件 | 内容 |
|---|---|
| `app.db` | **渠道的 SMTP key / bot token 是明文** |
| `state.json` | Playwright 的 storage state，**含完整登录态 cookie** |

备份它们等于备份凭证。放到不受控的位置要按凭证文件对待。

---

## 数据增长与保留策略

**结论：全量永久保留，不做降采样。**

实测的分表单行开销（`dbstat`，含索引）：

| 表 | 字节/行 | 增长条件 |
|---|---|---|
| `item` | **1813** | 每见到一个新商品 |
| `pricesnapshot` | **202** | 只在价格或状态变化时写 |
| `monitorhit` | 125 | 每条规则 × 商品一行 |

外推（标注：是外推，不是跑满 24 小时的实测）：1 条规则 1 年 8.9 MB；10 条规则每天变价 2 次
1 年 **177 MB**。SQLite 毫无压力，而价格历史是这个产品最值钱的资产——「这个价到底算不算便宜」
只有历史能回答。

**方向提醒**：无界增长的大头是 **`item`（占 1 年增量的 71%）**，不是快照。真要清理，第一个
该动的是**已不被任何命中或收藏引用的 `item` 的 `description` 与 `image_urls`**，
不是价格历史。

**重新评估触发条件**：库超过 1 GB，或单个商品的价格序列超过 1000 点。

---

## 故障 → 该做什么

面板上的每种状态对应一个动作。规则列表的「状态」列和设置页的会话状态是唯一需要看的两处。

| 现象 | 含义 | 动作 |
|---|---|---|
| 规则显示 `采集报错`（黄） | 暂时性失败，正在退避重试 | 等。看 `last_error` 里的 `transient:` 前缀 |
| 规则显示 `已停用 · 报错`（红） | 已自动停用。**风控类会在重新导入凭证时自动恢复；其它类不会自己回来** | 看原因；风控类重导凭证即可，其它类修好后手动启用 |
| 规则显示 `建立基线`（黄） | 首轮不推送，防止把存量当新命中 | 等一轮 |
| 规则显示 `无渠道`（黄） | **命中不会推送给任何人** | 去规则里勾选渠道 |
| 设置页「需要人工验证」 | 某个端点被风控挑战，**自动重试不会好转** | 按上面的步骤重新过验证并导入 cookie；同样内容也已推到通知渠道 |
| 通知里收到「采集已暂停：需要人工验证」 | 同上，一次挑战只发一条 | 按通知正文的五步做；导入后被风控停用的规则会自动恢复 |
| 设置页「凭证已导入，但还没有被验证过」 | 只是收下了 cookie，还没成功过 | 点一次「立即运行」 |
| 命中列表空且规则报错 | 是采集坏了，不是没找到 | 界面会直接显示错误态而不是空态 |
| 商品带 `地区未知` 等黄标注 | 该筛选条件因数据缺失**被保守放行** | 命中不代表真的满足这条筛选，自己看一眼 |
| 卖家画像显示「未知」 | 没抓到，**不是 0** | 0 条评价是危险信号，未知只是没数据，两者不同 |
| 收藏夹条目 `检查报错` | 单品检查失败 | 多为详情端点被挑战，重导凭证 |
| 渠道发送记录 `失败` | 看错误原文 | `535` 查 SMTP key 与端口；`401` 查 bot token |

---

## 想先看看界面而不碰上游

```bash
uv run python scripts/seed_demo.py --reset --break-one-image
```

从 `tests/fixtures` 的真实抓取生成演示数据（真商品 id、真图片、真卖家），建的演示规则是
**停用状态**，不会打上游。价格历史的早期点是从真实现价合成的，脚本会在输出里说明。

---

## 开发

```bash
uv run uvicorn app.main:app --reload --port 8000     # 后端
cd web && npm run dev                                 # 前端 → :5173（有 HMR，代理 /api）
cd web && npm run gen:api                             # 改了接口后重新生成前端类型
```

`gen:api` 不需要后端在跑——直接从 `app.openapi()` 导出，所以不会从一个还在跑旧代码的服务
上生成出过时的类型。

### 检查

```bash
uv run ruff check . && uv run mypy app && uv run pytest -q
cd web && npm run lint && npm run typecheck && npm run test && npm run build
```

`git config core.hooksPath .githooks` 装一次，`commit-msg` 会校验提交规范。

### 一个会让人困惑的测试环境行为

**TanStack Query 在窗口失焦时暂停重试。** 一个失败后停在 `isPending` 不动、Network 里也看不到
重试的查询，通常不是页面的 bug，而是浏览器窗口在后台。用自动化验证错误态时要用**真正获得焦点
的窗口**；或者改用 mutation 来验——mutation 默认不重试。
