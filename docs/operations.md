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
（书签那条路还会带一份环境快照，落在 `data/fingerprint.json`，同样只回一句摘要——见下。）

### 三条路，各有各的定位

| 路径 | 装什么 | 拿得到 `_m_h5_tk` | 明文 http 的局域网面板 |
|---|---|---|---|
| 开发者工具粘贴 | 无 | 看你复制的那个请求 | 可以（人肉搬运） |
| 书签脚本 | 拖一个书签到书签栏 | **不行** | **不行**（混合内容，浏览器拦死） |
| Chrome 扩展 | 装 unpacked 扩展 | **可以** | 本机 loopback 可以（实测）；局域网**预期**可以，见下 |

**开发者工具那条永远是兜底**：另外两条出任何问题都退回它。下面两节讲另外两条。

### 快捷方式：书签脚本

上面那五步每次都得走一遍开发者工具。设置页有个「生成书签」按钮，给你一个拖进书签栏的链接；
之后在**已登录的闲鱼页面**上点它，它把 `document.cookie` **和一份浏览器环境快照**直接 POST
回面板，页面左下角出现一行结果。

**它不取代上面的流程，也不总是能用**：

- 它只拿得到脚本所在那个域可读的 cookie。实测 44 个里有 43 个读得到（`XSRF-TOKEN` 是
  HttpOnly，读不到也不需要）。签名用的 `_m_h5_tk` 只存在于 `.taobao.com`，闲鱼页面上读不到。
  所以书签导入之后，设置页会显示**「会话不可用」**、原因写着 `no _m_h5_tk in adopted
  cookies`——这是**预期的**，不是导入失败。
- 少的这个 token **不是靠 token 轮换补回来的**（轮换只能换掉一个已经存在的 token；`_m_h5_tk`
  完全没有时 `MtopClient` 直接拒签，报 `no _m_h5_tk: session not established`）。补回来靠的是
  **浏览器兜底**：下一个周期看到会话不可用就走 Playwright，真实加载页面时闲鱼自己的 JS 会去
  领一个 `_m_h5_tk`，`_adopt_session()` 再把它交回内存会话。所以**书签导入后的第一个周期是
  一次较慢的、只看第 1 页的浏览器周期**，之后就回到便宜的 mtop 路径。
  （这条链子是书签路子成立的全部前提，`tests/test_import_ticket.py` 最后一个测试钉着它。）
- 闲鱼页面的 CSP 有可能拦掉这次跨源 `fetch`。拦了就退回开发者工具那条路，脚本会这么提示你。
- **面板走 http、而且不在 localhost 上时，这条路根本用不了**：闲鱼页面是 https，浏览器不允许
  它去 fetch 一个明文 http 地址（混合内容），`localhost` / `127.0.0.1` 是唯一的例外。
  所以：本机跑 → 能用；`http://192.168.x.x:8000` → 用不了，走开发者工具或下面的扩展；给面板
  配上 https → 能用。这个拦在浏览器里，后端加什么头都绕不过去。
- 走开发者工具复制的是那个请求**实际发出**的 Cookie 头，域一定对、字段一定全。所以它才是
  兜底的那条，出任何问题都用它。

#### 环境快照：让请求和凭证来自同一台机器

凭证是你自己浏览器里的，请求却一直带着另一套身份发出去：两个采集路径各写死了一份
`Chrome/131 · macOS · 1440×900 · zh-CN · Asia/Shanghai`。书签脚本现在顺手把这台浏览器
**页面上已经读得到的只读属性**捎回来（UA、`navigator.userAgentData`、语言列表、时区、
屏幕尺寸与像素比、CPU 核数、内存档位、触点数），两条采集路径都按它来。**不做任何额外探测**
——没有画布指纹、没有字体枚举、没有一次多余的请求。

- **快照存在 `data/fingerprint.json`，活过重启。** 不写进 `data/state.json`：那个文件的格式
  是 Playwright 的，由它自己序列化。
- **API 与页面只给一句摘要**（`Google Chrome 141 · Windows · 1920×1080 · Asia/Shanghai`），
  跟「只返回 cookie 名字」是同一条规矩。核数、内存这些原始字段留在后端。
- **开发者工具那条路没有快照，行为和以前逐字节一致**，继续用那份内置默认值。
- **手机上点书签，快照会存下来但不会被采用**，设置页会说明原因：本工具驱动的每个页面和接口
  都是 PC 版（`pc.search` / `pc.detail`），带着手机 UA 去请求 PC 接口只会更不一致。想让它
  生效，去电脑浏览器上再点一次。

> 这条改动**是否降低被风控的概率，本地无法验证**，也不要当成它会。它消除的是一个可以指出来
> 的自相矛盾，仅此而已。

**书签里带的不是面板 token，是一张一次性票据**：那段脚本在阿里控制的页面里执行，页面上的
任何东西都能读到它发的内容。票据只能换一次 cookie 导入，10 分钟过期，换不了别的任何东西
（进程重启也全部作废）。用过或过期后回设置页再点一次「重新生成」——旧书签会明确告诉你是
「过期」还是「已用过」，这两者的区别就是「再点一次按钮」和「这个书签来路不对」。

**判定标准不变**：导入成功只代表 cookie 收下了，还是要去「监控任务」点一次「立即运行」。
脚本在页面上打的那行字也是这么说的，它不会说「已恢复」。

> 滑块本身**只能你自己在浏览器里过**。面板不会替你解、也不会把验证转发到别处——把挑战搬出
> 它的来源好让自动采集绕过去，那是绕过机器人检测，这个项目不做。书签脚本做的事只是在你自己
> 已经登录、已经过完滑块之后，省掉复制粘贴。

### 另一条快捷方式：Chrome 扩展（`extension/`）

书签脚本已经够用的话就不用装。扩展只在**两件事**上真的更强：

1. **它拿得到 `_m_h5_tk`。** 扩展在 manifest 里同时声明了 `*://*.goofish.com/*` 和
   `*://*.taobao.com/*`，`chrome.cookies.getAll` 因此能读到 HttpOnly 和跨域的 cookie——
   包括只存在于 `.taobao.com` 的签名 token，那是页面上的 `document.cookie` 永远读不到的。
   所以**扩展导入完 mtop 直接可用，没有书签脚本之后那个只看第 1 页的浏览器兜底周期**。
   （这条有测试撑着：`tests/test_extension_import.py`，和钉住书签脚本那条链子的
   `tests/test_import_ticket.py` 最后一个测试互为镜像。）
2. **它的请求不受 CORS 和混合内容限制。** 网络请求发在 service worker 里，不在任何页面的源
   里，所以后端**一行 CORS 都没有为它改过**——`ALLOWED_IMPORT_ORIGINS` 仍然只服务书签脚本。

除此之外没有别的理由。它不抓 `localStorage`/`sessionStorage`，也不用 `webRequest`。

#### 装它

1. Chrome → `chrome://extensions` → 打开右上角「开发者模式」
2. 「加载已解压的扩展程序」→ 选本仓库的 `extension/` 目录（没有构建步骤，选了就能用）
3. 点扩展图标，填**面板地址**和 **API token**（就是 `SFD_API_TOKEN`），点「保存」
4. 在**已登录闲鱼的标签页**上点「导入」。Chrome 会弹一次授权框问是否允许访问你填的那个面板
   地址——这是运行时申请的，只针对你填的那台机器，不是「所有网站」

弹窗会告诉你取到了多少个 cookie、**里面有没有 `_m_h5_tk`**，以及有几个 cookie 在两个域上同名
（同名的按名字去重、保留 goofish 那份；两边值不一样的会单独报数）。它**不显示任何 cookie 值**。

#### 两个第一次装就会踩的坑（2026-09-08 实测踩到）

**一、面板地址填 `localhost` 连不上，填 `127.0.0.1` 就通。**
`localhost` 在 macOS 上先解析到 IPv6 的 `::1`，而 `uvicorn` 默认只绑 IPv4 `127.0.0.1`，
所以 fetch 打的是一个关着的 IPv6 端口。`curl` 会自己回退到 IPv4，所以命令行测起来一切正常
——这就是最误导的地方：**面板明明在跑**。填 `127.0.0.1` 即可；弹窗现在也会直接这么提示你。

**二、要在闲鱼标签页上点「导入」，不是在面板标签页上点。**
cookie 走的是 cookie store，你在哪个标签页点都能拿到，所以导入照样报成功。但**环境快照是往
当前标签页里注入脚本读的**，而注入需要那个标签页的 host 权限——面板和 goofish 是不同的 host，
在面板页上点就注入不了。结果是：导入成功、cookie 齐全、**但设置页的「采集端身份」还是「内置
默认值」**。弹窗现在会说清楚原因。

**导入连续失败、而 `/api/health` 通得了**，说明面板活着、是这一个请求被拦了：多半是扩展的
host 权限没生效（**重新加载 unpacked 扩展会把运行时授予的权限清掉**）。去
`chrome://extensions` 重新加载本扩展，再点一次「导入」并允许。
底层原因是：没有那个权限时 Chrome 就不把它当扩展的特权请求，而是当普通跨源请求发，于是面板
收到一个预检——那条路由没有 `OPTIONS`、扩展源也不在 `ALLOWED_IMPORT_ORIGINS` 里——回 405，
`fetch` 只报一句 `Failed to fetch`。

#### 权限清单（就这四个）

`cookies`（读 cookie）、`storage`（存你填的面板地址和 token）、`scripting`（在闲鱼页面上读一份
环境快照）、`tabs`（知道你当前在哪个标签页）。host 权限只有 goofish 和 taobao 两个域；面板地址
在 `optional_host_permissions` 里，点「导入」时才申请。

**没有 `webRequest`**：参考实现用它去抓真实的 `sec-ch-ua*` 请求头，代价是安装时授权框上多一条
「读取你的浏览记录」。这里不需要——面板本来就从 `navigator.userAgentData.brands` 重建那几个头
（`app/collector/fingerprint.py`）。

#### Local Network Access：扩展**确实**受限（2026-09-08 实测）

Chrome 的 Local Network Access 限制向本地网络地址发请求，而**扩展不豁免**。官方文档没写这
一点，是实测撞出来的。两条结论，第二条是坑：

**一、地址空间有三个，不是两个**：`public`、`local`（RFC 1918 与 IPv6 unique-local）、
`loopback`（`127.0.0.0/8`、`::1`、以及解析到它们的名字）。

**二、`targetAddressSpace` 是断言，不是提示。** 声明错了 Chrome **直接拒绝**，不是忽略也不是
降级：

```
blocked by CORS policy: Request had a target IP address space of `local`
yet the resource is in address space `loopback`
```

第一版把 `127.0.0.1` 当成 `local` 声明了——而那正是自托管面板真实跑的地址，所以这个错会
100% 命中真实用户，症状是弹窗只说一句 `Failed to fetch`。现在 `extension/lib/panel.js` 的
`addressSpace()` 按三态分类，公网地址则**一个断言都不发**（往那个方向声明同样是假话）。

局域网明文 http 面板仍然是「预期可以，实测为准」——自己试一次。

#### 它往哪儿发

只往**你在弹窗里填的那个地址**发，打的是和另外两条路一样的 `POST /api/session/cookies`。
代码里没有任何其它出网目的地：没有遥测、没有更新检查、没有第三方。
`web/src/lib/extension.test.ts` 里那条「outbound destinations」测试把 `extension/` 下每一个
绝对 URL 逐条列了出来并整体断言，加一个进去测试就红。

**票据机制这条路不用**：service worker 不在闲鱼页面里跑，页面上的 JS 读不到它发的东西，所以它
带的是面板自己的 bearer token。要每次先去设置页换一张票据的话，扩展就不比书签省事了。

**判定标准照旧**：导入成功只代表 cookie 收下了，还是要去「监控任务」点一次「立即运行」。
弹窗上那行字也是这么写的，它不会说「已恢复」。

### 被风控之后

不需要盯面板：**采集一被挑战就会往所有启用的渠道发一条通知**，正文就是上面这五步。
（走所有启用渠道而不是规则关联的渠道——采集停的是全局，没关联渠道的规则否则会静默。
一次挑战只发一条，直到采集重新成功或重新导入凭证为止。）

上面那五步在代码里也有一份：`app/notify/base.py` 的 `CHALLENGE_STEPS`，改这里就一起改那里。

**重新导入后会自动做的事**：清掉挑战标记，把**被风控停用的监控规则和收藏夹条目**重新启用
（只有这一类；手动停用的、和连续失败 5 次停用的都不动），并清掉**被这次风控污染的卖家画像
标记**。

最后这条需要解释一下：画像抓取失败时会写 `seller.fetch_error`，同时**也会动
`fetched_at`**——否则每个周期都会重买一个刚刚失败的请求。但那个时间戳是拿 7 天画像 TTL 去
读的，所以如果不清，一次风控就会让当时筛过的所有卖家**整整一周没有画像**，而且是静默的。
只清风控这一类（`fetch_error` 以 `challenged:` 开头）；卖家自己的问题（商品已删之类）是
真的黏性失败，标记保留。导入**不会**立刻触发采集——所有规则一起打上游正是
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
| 卖家画像显示「未知」 | 没抓到，**不是 0** | 0 条评价是危险信号，未知只是没数据，两者不同；想知道为什么没抓到，看 `seller.fetch_error` |
| 收藏夹条目 `检查报错` | 单品检查失败 | 多为详情端点被挑战，重导凭证 |
| 渠道发送记录 `失败` | 看错误原文 | `535` 查 SMTP key 与端口；`401` 查 bot token |

---

## 运维备注：一行零引用的数字 id 卖家（可删）

`8f10a77` 之前的代码把详情响应里的**数字** `sellerId` 写进了 `seller.id`，而商品外键指向的
是搜索给的**不透明 base64** id。真库 253 行里留下两行数字 id，**处置不同，不要一起删**：

| id | 昵称 | 有无不透明孪生行 | 被几个 item 引用 | 处置 |
|---|---|---|---|---|
| `195943285` | 中国村长 | 有（`fsoDU51KCfayFJu+Q5EScA==`，画像值完全相同） | 0 | **删** |
| `2218219939144` | 小顾数码 | **没有** | 1（`1079129727205`） | **留着** |

删那一行（停机后做，`data/app.db` 先备份 —— 见上面「备份与恢复」，运行中 `cp` 不算备份）：

```sql
-- 先确认它真的零引用，返回 0 才继续
SELECT count(*) FROM item WHERE seller_id = '195943285';
DELETE FROM seller WHERE id = '195943285';
```

**`2218219939144` 不能删**：它是一个 item 的活外键，删了破坏引用完整性；而且它没有不透明
孪生行，没有可合并的目标。它顺带说明数字 id **能**出现在 `item.seller_id` 上——id 空间不是
纯不透明的，这是留给卖家列表端点探测的线索。

一行 SQL 的事，没写迁移脚本：253 行里 1 行，写脚本的代码比 SQL 长。

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
