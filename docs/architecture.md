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

## 数据模型（11 张表）

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
| `LlmEndpoint` / `LlmScenarioConfig` | M4 的配置面，表已建，暂无代码读取 |

### 为什么快照只在变化时写

行量因此跟的是**变价频率**而不是轮询频率。实测：一条规则连续跑 9.2 分钟（约 60 次观测）
新增 **0 条**快照；全库 131 个商品里 124 个只有 1 条快照。代价是查「当前价」要一次查询而不是
读一列——`store.latest_snapshots` 用一个窗口函数一次解决，与行数无关。

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
├── pages/       六个页面，单用组件留在页内
├── components/  States(三态) / RemoteImage / SellerProfile / PriceChart
├── lib/         format.ts(分转元唯一出口) · chart.ts(阶梯几何，纯函数)
└── styles/      tokens.css · base.css
```

五条会造成最大破坏的约定：

1. `api/types.ts` 由 OpenAPI **生成**，手写的后端类型一定会漂移并撒谎
2. 查询键只从 `api/queries.ts` 的工厂产出，手打的键与失效不匹配会静默冻结界面
3. 筛选态入 URL，分享或刷新必须复现同一视图
4. **加载 / 错误 / 空是三种独立状态**——对监控工具，「没找到」和「采集坏了」不能长得一样
5. 价格是整数分，只由 `lib/format.ts` 转显示

价格图是**手写 SVG 阶梯线**：一种图表不值得引一个比整个应用还大的依赖，而且阶梯语义
（价格在下次观测前一直保持）不能画成平滑曲线——那会画出从未存在过的价格。

---

## 接口

完整契约以 OpenAPI 为准，运行后看 **`/docs`**（Swagger UI）或 `/openapi.json`。
前端类型由它生成，所以文档与代码不可能不一致。

24 个端点，除 `/api/health` 外全部需要 `Authorization: Bearer <SFD_API_TOKEN>`：

```
监控规则   GET/POST /api/monitors · GET/PATCH/DELETE /api/monitors/{id} · POST /api/monitors/{id}/run
命中商品   GET /api/items · GET /api/items/{id} · GET /api/items/{id}/prices
收藏夹     GET/POST /api/watchlist · PATCH/DELETE /api/watchlist/{item_id}
通知渠道   GET/POST /api/channels · PATCH/DELETE /api/channels/{id} · POST /api/channels/{id}/test
           GET /api/notify-logs
采集会话   GET /api/session · POST/DELETE /api/session/cookies
其它       GET /api/health（免认证，容器健康检查用） · GET /api/whoami
```

`/api/health` 刻意免认证：它要能作为容器健康检查。
