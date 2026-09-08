# 架构

> 一个自托管的闲鱼监控工具：持续采集选定关键词/商品，命中预算时实时推送，并追踪收藏商品的降价。

产品的价值从**通知**出去。web 面板是配规则、看采集有没有坏、管收藏夹的**操作台**，
按使用频率算一天开几分钟，不是盯着看的东西。

---

## 进程模型

单进程，一个 FastAPI 应用，`lifespan` 拥有全部长生命周期资源：

```
uvicorn app.main:app
├── SQLite（WAL，data/app.db）
├── UpstreamSession        采集会话（cookie jar + 按端点的风控状态）
├── MtopClient             便宜路径：直接签名调 h5 API
├── BrowserCollector       一个 Playwright 浏览器 + 一个持久上下文
├── search_loop            关键词规则轮询
└── watch_loop             收藏商品轮询
```

两条循环**共用一个 `asyncio.Semaphore(1)`**：两类监控节奏差 5 倍，单循环会让收藏项被关键词
规则拖慢；共用信号量则保证对上游的并发始终是 1，这是防封的第一道闸。

浏览器**每进程只启一次**。按轮次启动浏览器是这个代码库里最贵的一个错误。

---

## 采集：三条路径与降级

上游是 `h5api.m.goofish.com` 的 mtop 接口，签名是
`md5(f"{token}&{t}&{app_key}&{data}")`，`token` 取自 `_m_h5_tk` cookie 的前缀。

| 路径 | 用途 | 独立性 |
|---|---|---|
| mtop 直接调用 | 主路径，最便宜 | — |
| 浏览器拦截同名 XHR | 搜索的兜底 | 搜索**有** DOM 兜底，详情**没有** |
| 抓 DOM | 只有搜索有选择器 | 详情页被挑战时渲染的是推荐流，抓不到目标商品 |

**浏览器是搜索的兜底，不是详情的兜底。** 详情的浏览器路径拦截的是
`mtop.taobao.idle.pc.detail` ——和便宜路径同一个 API，所以该端点被挑战时两条都不通；
而给详情加 DOM 兜底会抓到**推荐位商品的价格挂在请求的 id 下**，对比价工具比报错更糟。
详见 `.trellis/spec/backend/collector-guidelines.md`。

### 会话与风控

- **门在会话层**：同一台机器同一 IP，无会话必被 `RGV587_ERROR` 挑战，带一份已登录 cookie
  则正常返回。所以唯一的硬前置是**导入有效登录态 cookie**，部署位置不是硬约束。
- **`_m_h5_tk` 会轮换**：服务器在每个响应（含报错响应）里下发新 token。代码吸收它并**重签一次**
  重试，这让「token 过期」自愈，而不是要人手动重新导入。
- **风控按端点隔离**：实测过详情端点被挑战而搜索照常工作。所以 `challenged_apis` 是
  `dict[api, reason]` 而不是一个全局开关——否则一次收藏夹查询失败会把健康的关键词规则全停掉。
- **挑战 ≠ 限流**：`ChallengeError` 第一次就停用规则并暴露原因，其它失败才退避重试、数到 5 次停用。
  静默退避会让工具一直不干活而界面显示「正常」。

### 错误分类 → 动作

| 错误 | 动作 |
|---|---|
| `TransientCollectorError` | 退避重试，`last_error` 记 `transient:` 前缀 |
| `ChallengeError` | **立即停用**该规则并保留原因，标记该端点 |
| `TokenStaleError` | 吸收新 token，重签重试一次 |
| `ItemGoneError` | 不是失败：**消失本身就是观测**，标记状态并推送 `gone` |
| 其它 `CollectorError` | 退避重试，连续 5 次后停用 |

---

## 数据模型（12 张表）

全部在 `app/models.py`。两条贯穿全局的不变量：

- **金额一律整数分**（`price_cents`），前端只由 `lib/format.ts` 一处转显示
- **时间一律 UTC aware**，靠 `UtcDateTime` 这个 `TypeDecorator` 双向强制
  （SQLite 存回来是 naive 的，`DateTime(timezone=True)` 也一样，实测过）

| 表 | 作用 |
|---|---|
| `Monitor` | 一条关键词规则：九项筛选 + 间隔 + 健康字段（`last_error` / `last_collector` / `consecutive_failures`） |
| `Item` | 见过的商品，含展示字段（封面、图片 JSON、地区、状态） |
| `Seller` | 卖家画像。**每个字段都可空，`None` 是「没抓到」不是 0** |
| `PriceSnapshot` | **append-only 价格历史，只在价格或状态变化时写** |
| `MonitorHit` | 去重与通知账本，`(monitor_id, item_id)` 复合主键 |
| `Watchlist` | 收藏夹，独立轮询间隔，与规则解耦 |
| `NotifyChannel` | 渠道配置（密钥明文存此，见运维文档） |
| `MonitorChannel` | 规则 ↔ 渠道多对多。**空关联 = 这条规则不通知任何人** |
| `NotifyLog` | 发送记录，含失败原因 |
| `CollectRun` | **每个采集周期一行**，成功与失败都写。见下 |
| `LlmEndpoint` / `LlmScenarioConfig` | M4 的配置面，表已建，暂无代码读取 |

### 为什么快照只在变化时写

行量因此跟的是**变价频率**而不是轮询频率。实测：一条规则连续跑 9.2 分钟（约 60 次观测）
新增 **0 条**快照；开发库在 2026-09-05 是 328 个商品 343 条快照，其中 317 个只有 1 条
（这三个数每采集一轮就会变，看的是比例不是绝对值）。代价是查「当前价」
要一次查询而不是读一列——`store.snapshot_ids_at()` 用一个窗口函数一次解决，与行数无关。

**代价之二，M2 才付出来**：快照的**存在性**因此不能用来判断「那天这件在不在售」。
一件从头到尾没变价的商品和一件早就下架的商品，快照行数一样多（都是 1 条）。在售看
`Item.last_seen_at` 的新鲜度；`Item.status` 也不行——它对关键词商品恒为 `on_sale`
（2026-09-05 实测 328 件里 **0 件**例外），只有收藏项的详情抓取会改它，所以
`status` 只用来**排除**真正观测到已售/下架的那几件。

### `CollectRun`：「那天没新货」和「那天没采集」是两件事

只看 `Item` / `PriceSnapshot`，这两种情况长得完全一样，供应量趋势图会把它们画成同一根
零柱——也就是进程挂了三天，图上显示市场冷了三天。`CollectRun` 每周期一行（成功和四个失败
分支都写），是「那天到底采没采」的唯一依据。它早于 M2 的日期一律画成「未采集」，这是准确的：
我们确实没有那几天的采集记录，而记录无法事后补。

- **全量保留，不降采样**，与快照同一个决定。300s 间隔下约 288 行/天/规则。
- **写日志用自己的 session、自己吞异常**（`scheduler._sync_record_run`）。与业务写共用事务
  的话，一次失败的日志写会把它本该只是观测的采集一起回滚——能毁掉被观测对象的观测手段，
  比没有观测手段更糟。

---

## 命中判定：「价格**进入**区间」

这是整个 M1 最吃逻辑的一处，在 `store.evaluate_hit`。

判据不是「有没有见过这个商品」，而是 `MonitorHit.in_range` 的**状态转换**：

| 转换 | 动作 |
|---|---|
| 首次进入区间 | 推送 `new_in_range` |
| 离开区间 | 只记状态，不推 |
| 重新进入 | 推送 `new_in_range`——它可能是超预算后降价进来的，**这是捡漏最常见的形态** |
| 区间内继续下跌超过 `price_drop_ratio` | 推送 `price_drop` |
| 在区间内但从未成功宣布过 | 重试推送（上次发送失败了） |

只按「见过没见过」去重，会**永久漏掉**一个先超预算、后降价进区间的商品。

两个配套机制：

- **首轮只建基线**（`_stamp_as_known`）：新规则第一轮把存量商品标记为「已知」而不推送，
  否则一上线就把几十条存量当成新命中全推一遍
- **写在发送之后**：`notified_at` / `notified_price_cents` 在发送成功后与 `NotifyLog` 同事务写入。
  先写后发意味着崩溃的发送永久静默丢失；后写最多重复一次，对告警工具这是正确的取舍

---

## 通知

`Notifier` 协议 + 一个 `Notification` 载荷，加渠道就是加一个文件。`reason` 四个取值：
`new_in_range` / `price_drop` / `gone`，以及只出现在 `NotifyLog.kind` 上的 `test`。

- **一轮一条，合并发送**：十条命中发一条含十项的消息，不是十条消息。这是「有用的告警」和
  「被静音的渠道」之间的区别。实测过一次 9 条合成一封。
- **规则的渠道要显式关联**。空关联就是不通知任何人，面板会在列表里标 `无渠道` 警告。
  （收藏夹降价走「所有启用渠道」，因为被收藏的商品不属于任何规则。）
- **保守放行必须标注**：筛选条件因数据缺失无法验证时放行商品，但把
  `unverified_filters` 一路带到列表和推送正文（如「卖家信用未知」），否则「保守放行」
  会退化成静默误推。

---

## 前端

Vite + React 19 + TS，plain CSS + 设计令牌，无组件库、无图表库、无 webfont。

```
web/src/
├── api/{types.ts(生成), client.ts, queries.ts}
├── pages/       八个页面，单用组件留在页内
├── components/  States(三态) / RemoteImage / SellerProfile / PriceChart / Histogram / DailyBars
├── lib/         format.ts(分转元唯一出口) · chart.ts(图形几何，纯函数，三张图共用)
└── styles/      tokens.css · base.css
```

五条会造成最大破坏的约定：

1. `api/types.ts` 由 OpenAPI **生成**，手写的后端类型一定会漂移并撒谎
2. 查询键只从 `api/queries.ts` 的工厂产出，手打的键与失效不匹配会静默冻结界面
3. 筛选态入 URL，分享或刷新必须复现同一视图
4. **加载 / 错误 / 空是三种独立状态**——对监控工具，「没找到」和「采集坏了」不能长得一样
5. 价格是整数分，只由 `lib/format.ts` 转显示

价格图是**手写 SVG 阶梯线**：一种图表不值得引一个比整个应用还大的依赖，而且阶梯语义
（价格在下次观测前一直保持）不能画成平滑曲线——那会画出从未存在过的价格。M2 的直方图与
按日柱状同理：都是线性刻度上的矩形，与阶梯线共用同一个 `scale()`，各约 40 行。该换图表库
的触发点不是「第三种图」，而是需要缩放/刷选，或出现非矩形几何。

「未采集」用**斜纹图案**而不是只换颜色，降幅用 `▼` 加数字而不是只用绿色：颜色不单独承载
含义。每张 SVG 配 `role="img"` + `aria-label`，下方给同数据的表格——SVG 回答不了「哪天」，
表格能，而且屏幕阅读器只拿得到表格。

---

## 接口

完整契约以 OpenAPI 为准，运行后看 **`/docs`**（Swagger UI）或 `/openapi.json`。
前端类型由它生成，所以文档与代码不可能不一致。

29 条路径上 40 个操作（2026-09-08 从 `app.openapi()` 数的）。除下面两条例外，全部需要
`Authorization: Bearer <SFD_API_TOKEN>`：

```
监控规则   GET/POST /api/monitors · GET/PATCH/DELETE /api/monitors/{id} · POST /api/monitors/{id}/run
命中商品   GET /api/items · GET /api/items/{id} · GET /api/items/{id}/prices
收藏夹     GET/POST /api/watchlist · PATCH/DELETE /api/watchlist/{item_id}
通知渠道   GET/POST /api/channels · PATCH/DELETE /api/channels/{id} · POST /api/channels/{id}/test
           GET /api/notify-logs
采集会话   GET /api/session · POST/DELETE /api/session/cookies · POST /api/session/import-ticket
行情分析   GET /api/analytics/price-distribution · /price-drops · /supply-trend · /listing-duration
LLM        GET/POST /api/llm/endpoints · PATCH/DELETE /api/llm/endpoints/{id}
           GET /api/llm/endpoints/{id}/models · POST /api/llm/endpoints/{id}/test
           GET/PUT /api/llm/scenarios/{scenario} · GET /api/llm/scenarios/{scenario}/default-prompt
           POST /api/llm/analyze/market · POST /api/llm/analyze/item/{item_id}
其它       GET /api/health（免认证，容器健康检查用） · GET /api/whoami
```

两条例外：`GET /api/health` 免认证；`POST /api/session/cookies` 收 bearer token **或者**
一张一次性导入票据（`X-Sfd-Import-Ticket`，由 `POST /api/session/import-ticket` 用 bearer
token 换来，10 分钟、只能用一次、只开这一扇门）。票据存在于进程内存，不落库——重启即全部作废。

它也是全应用**唯一**接受跨源调用的路由：`app/api/session.py` 的 `import_cors` 只给
`*.goofish.com` 那几个源、且只给这条路径发 CORS 头。之所以写成中间件而不是在处理函数里加
头，是因为最需要被读到的恰恰是失败响应——票据过期时若少了这个头，书签脚本只能显示一句
「网络错误」。用不了 `CORSMiddleware`：那是整应用一档的配置，而这里要的是单路由白名单。

三个行情端点都收 `days`，但**每个里它的含义不同**（这不是不一致，是各自的自然含义）：

| 端点 | `days` 限定什么 | 默认 |
|---|---|---|
| `price-distribution` | 哪些商品算进来：窗口内至少被看到过一次 | 7 |
| `price-drops` | 比价基准点：窗口起点的价 vs 现价 | 7 |
| `supply-trend` | 图表横轴跨度，窗口内每一天都返回 | 30 |

分布的默认是 7 天而不是「只看此刻在售」，因为一个周期只读搜索前 30 条，此刻在观测范围内的
永远只有约 30 件/关键词。响应里的 `fresh_size` 才是「其中多少件最近仍被看到」。

日界是 **UTC** 日界（列里存的就是 UTC），时区转换在前端做。

`/api/health` 刻意免认证：它要能作为容器健康检查。
