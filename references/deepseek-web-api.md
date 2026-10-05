# DeepSeek Web 内部接口参考

本文件记录 `chat.deepseek.com` 网页版内部接口的实测结论，供排查与二次开发时查阅。所有结论基于 2026-10-05 对真实账号的抓取，路径均从官方前端 `main.js` 提取，非猜测。

## 1. 登录态来源

DeepSeek Web **不使用 session cookie**。`cookies.sqlite` 里 `chat.deepseek.com` 只有 `smidV2` 与一个 thumbcache 项，都是匿名标识。

真正的令牌在 localStorage 的 `userToken` 键：

```
{"value":"<64 位 token>","__version":"0"}
```

Firefox 新版（115 及以上）已把 localStorage 从 `webappsstore.sqlite` 迁到：

```
<profile>/storage/default/https+++chat.deepseek.com/ls/data.sqlite
```

目录名转义规则：`://` 换成 `+++`，其余 `/` 换成 `+`。老的 `webappsstore.sqlite` 里该站点零记录，只查它会得出「未登录」的错误结论。SQLite 源文件被运行中的 Firefox 独占，读取前必须先复制到临时目录。

Edge / Chrome 不可用：cookie 有 App-Bound Encryption，且令牌根本不在 cookie 里。

## 2. 请求头

鉴权与客户端标识：

| 头 | 值 | 说明 |
| --- | --- | --- |
| `Authorization` | `Bearer <userToken>` | 令牌本体 |
| `x-client-version` | `2.5.0` | 是 appVersion，**不是** commit-id |
| `x-client-bundle-id` | `com.deepseek.chat` | 由 `HP(brand)` 映射，brand 默认 `deepseek` |
| `x-client-platform` | `web` | |
| `x-client-locale` | `zh_CN` | |
| `x-client-timezone-offset` | `28800` | `60 * utcOffset 分钟`，东八区为正数 |
| `x-device-id` | localStorage 的 `deepseek-device-id:chat` | 缺失时可用固定 UUID 兜底 |
| `Origin` / `Referer` | `https://chat.deepseek.com` | |

**填错任何一个都不会报错**，服务端照回 200，但会静默降级：

- 列表项被裁成只有 `id` / `title` / `title_type` / `updated_at` 四个字段
- `pinned` 与 `model_type` 两个字段消失
- `pinned` 查询参数被整个忽略
- 翻页游标被忽略，永远返回同一批

症状表现为「数据看着正常但少字段、翻页翻不动」，极易误判成接口本身的限制。排查数据异常时，先核对这三个头。

## 3. 接口清单

### 3.1 会话列表

```
GET /api/v0/chat_session/fetch_page
```

参数：

| 参数 | 说明 |
| --- | --- |
| `count` | 每页条数，合法区间 2 到 50。传 1 回 `biz_code=1 ILLEGAL_COUNT` |
| `gte_cursor.pinned` / `gte_cursor.updated_at` | 向后（升序）取，配合 `pinned=true` 拿置顶区 |
| `lte_cursor.pinned` / `lte_cursor.updated_at` | 向前（降序）取，拿普通区最新一批 |

返回 `biz_data.chat_sessions[]` 与 `biz_data.has_more`。

实测语义（该账号 1000 条会话）：

- 置顶区：`gte_cursor.pinned=true&gte_cursor.updated_at=0`，一次返回全部置顶（实测 6 条，`has_more=false`）
- 普通区首页：`lte_cursor.pinned=false&lte_cursor.updated_at=9999999999`（未来时间戳）
- 普通区后续页：把本页末条的 `updated_at` 作为新锚点继续 `lte`

方向弄反的两个坑：`gte + updated_at=0` 拿到的是**最旧**那批；`lte + updated_at=0` 直接返回 0 条。

置顶会话**不出现在 `pinned=false` 的查询结果里**，必须单独查一次。

### 3.2 会话正文

```
GET /api/v0/chat/history_messages
```

参数只有 `chat_session_id`。`cache_version` / `cache_reset_at` 是前端 IndexedDB 缓存优化字段，**传空串会回 400**（`cannot parse integer from empty string`），不传反而正常。

返回 `biz_data.chat_session` 与 `biz_data.chat_messages[]`。单次返回整个会话，实测 309 条消息约 1.1 MB。

### 3.3 分享列表

```
GET /api/v0/share/list
```

`count` 为**必填**，缺失回 422（`detail: query.count`）。`offset` 可选。

返回 `biz_data.shares[]`，每项含 `share_id` / `hint`（标题）/ `created_at` / `chat_session_id`。

### 3.4 分享内容

```
GET /api/v0/share/content
```

参数 `share_id`。返回 `biz_data.title` / `biz_data.messages[]` / `biz_data.model_type`。

`messages` 与 `chat_messages` 同构，但**没有 `current_message_id`**，且顶层消息的 `parent_id` 指向不在返回集合里的节点，需要把「父不在集合内」的消息当作根。

## 4. 消息树与分支

每条消息有 `message_id` 与 `parent_id`，`parent_id` 为 null 的是根。用户在网页上**编辑提问**或**重新生成回答**都会在同一父节点下挂一个兄弟消息，旧版本保留不删。

实测「GTNH MV」全树 309 条，而沿 `chat_session.current_message_id` 回溯出的当前分支只有 124 条，其余 185 条是废弃分支；「GTNH骗氪梗解析」全树 375 条、当前分支仅 46 条。

网页只显示当前分支这一条链，因此读对话做总结时应只取当前分支，否则会把用户已经改掉的说法当成真实对话内容。

## 5. 字段速查

| 字段 | 取值 | 含义 |
| --- | --- | --- |
| `chat_sessions[].pinned` | true / false | 是否置顶 |
| `chat_session.current_message_id` | 整数 | 当前活跃分支的末条消息 |
| `message.role` | `USER` / `ASSISTANT` | 说话方 |
| `message.status` | `FINISHED` / `INCOMPLETE` | 生成是否完成 |
| `message.thinking_enabled` | true / false | 该轮是否开启「深度思考」，**独立开关，与 model_type 无关** |
| `message.search_enabled` | true / false | 该轮是否开启「智能搜索」，同样是独立开关 |
| `fragment.type` | `REQUEST` / `RESPONSE` / `THINK` / `SEARCH` | 用户提问 / 回答 / 思考过程 / 联网搜索 |

正文统一放在 `fragments[]` 里，同一轮可能有多段，按数组顺序拼接。`--no-think` 只过滤掉 `THINK`。

### model_type 是历史遗留字段

`model_type` 记录的是会话创建时界面上的「快速 / 专家」模式开关，取值三种：

- `default`：快速模式
- `expert`：专家模式
- `vision`：识图模式（官方称「识图模式」，界面上与快速、专家并列的第三个入口）

**它与「深度思考」是两件事，不要混为一谈。** 深度思考与智能搜索是消息级的独立开关，分别记在 `message.thinking_enabled` 与 `message.search_enabled` 上，任何模式下都能单独打开。取证会话：2026-10-05 16:22 的「深度思考已开启确认」，`model_type` 为 `default`，而消息的 `thinking_enabled` 为 true 且带 THINK 片段。

该模式字段**现已停用**：DeepSeek 在 2026 年 9 月底把快速与专家模式合并，之后新会话恒为 `default`。实测该账号按月分布：

| 月份 | default | expert | vision |
| --- | --- | --- | --- |
| 2026-04 | 66 | 44 | 0 |
| 2026-05 | 86 | 103 | 0 |
| 2026-06 | 120 | 39 | 17 |
| 2026-07 | 170 | 54 | 18 |
| 2026-08 | 50 | 20 | 2 |
| 2026-09 | 168 | 9 | 2 |
| 2026-10 | 32 | 0 | 0 |

所以它只对 2026 年 9 月之前的历史会话有区分意义，展示时标注为历史遗留，不要当作"当前模型"。

参考文章：知乎《快速 vs 专家！DeepSeek 更新后差距一目了然》。

### 识图模式的公开时间线

`vision` 这个取值对应官方 2026 年新推的「识图模式」，公开节点如下：

| 时间 | 事件 |
| --- | --- |
| 2026-04-24 | DeepSeek-V4 预览版发布，技术报告预告未来融入多模态 |
| 2026-04-28 / 29 | 多模态团队负责人陈小康在 X 预告（鲸鱼图标单眼），网页端与 App 小范围灰度，入口标注「图片理解功能内测中」 |
| 2026-04-30 | 发布多模态技术报告《用视觉原语思考》（Thinking with Visual Primitives），随后仓库与论文原文被撤下 |
| 2026-05-09 | 大范围开放测试 |
| 2026-06-18 | 识图模式在 App 与网页端正式上线 |
| 2026-08-21 | API 侧上线实验模型 `deepseek-v4-flash-vision-exp`，08-31 开源 |
| 2026-09-08 | 官方群发布 DeepSeek V4.1 Flash 中间版本，原生支持多模态 |

技术要点：基于 DeepSeek-V4-Flash 的 284B 架构加自研 ViT 视觉编码器，把点、边界框等空间坐标当作推理的基本单元（即「视觉原语」），宣称 756×756 图像压缩比可达 7056 倍，实测 800×800 图片约消耗 90 tokens。局限集中在公开影像稀少的特定人物识别、计数与视错觉这类反直觉图形，以及长时间思考引发的逻辑幻觉。


## 6. 其他实测结论

- HTTP 200 不代表成功。必须同时看 `data.code` 与 `data.biz_code`，`biz_code` 非 0 时 `biz_data` 恒为 null。
- 429 是限流，连续快速翻页容易触发，隔一会儿重试。
- token 过期时回 401 / 403。重新登录网页版并刷新页面，`userToken` 会自动更新。
- 页面资源地址可从首页 HTML 的 `main.<hash>.js` 取得，接口路径用正则 `/api/v0/[A-Za-z0-9_/]+` 提取即可。
