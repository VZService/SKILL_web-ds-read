# web-ds-read

把 DeepSeek 网页版（chat.deepseek.com）的对话读成可阅读文本，让 AI 能看到你在 DeepSeek 上聊过什么，并据此总结、追问、接着聊。

**只读**：不发送消息、不删除会话、不改标题。

## 能拉什么

| 数据 | 说明 | 是否需登录 |
|---|---|---|
| 会话列表 | 标题 / 更新时间 / 置顶标记 / 会话 id，可翻全（实测账号上千条） | 需要 |
| 会话正文 | 完整问答，含思考过程与联网搜索片段 | 需要 |
| 历史分支 | 编辑提问、重新生成回答留下的旧版本 | 需要 |
| 分享链接 | 自己创建的分享及分享内对话 | 需要 |

## 前置条件

- 本机 Windows，Firefox 已登录 `chat.deepseek.com`
- 纯 Python 标准库，无需安装任何包，WorkBuddy 内置 Python 与系统 Python 均可运行

必须用 Firefox 的原因：DeepSeek 的登录令牌存在 localStorage 的 `userToken` 键里，**不在 cookie**。Firefox 的 localStorage 可直接读取，而 Edge / Chrome 的 cookie 受 App-Bound Encryption 保护，且该令牌本来也不在 cookie 中。

## 用法

```bash
# 先探登录态，可省掉一轮无效请求
python scripts/ds_read.py --token-only

# 列会话（置顶在前，默认 20 条）
python scripts/ds_read.py --list --limit 30

# 读最近更新的会话，建议加 --no-think 过滤思考过程
python scripts/ds_read.py --latest --no-think

# 读指定会话
python scripts/ds_read.py --session <会话 id>

# 按标题关键词挑（会拉最近 200 条再筛）
python scripts/ds_read.py --keyword "MV" --no-think

# 展开被编辑或重新生成换掉的旧分支
python scripts/ds_read.py --session <id> --all-branches

# 列出自己创建的分享链接、读某条分享
python scripts/ds_read.py --shares
python scripts/ds_read.py --share <share_id> --no-think
```

默认只打印到终端，加 `--save` 才落盘。

## 参数

| 参数 | 说明 |
|---|---|
| `--list` | 列会话，置顶在前、其余按更新时间倒序；`--limit` 控制条数（默认 20，可放大到上千，脚本按 50 条一页自动翻） |
| `--session <id>` | 读指定会话全文，id 为 32 位 UUID，先用 `--list` 取 |
| `--latest` | 读最近更新的会话（按更新时间取，不会被置顶顶掉） |
| `--keyword "词"` | 按标题关键词筛，命中多个时逐个输出 |
| `--shares` | 列出自己创建的分享链接 |
| `--share <share_id>` | 读取某条分享链接里的对话 |
| `--no-think` | 不输出思考过程，只要问答正文，读对话时建议默认加上 |
| `--all-branches` | 展开全部历史分支，分支点标 `[分支 k/n]`，当前分支标 `[分支 k/n, 当前]` |
| `--raw` | 输出原始 JSON，需自行看 `fragments` / 分支结构时用 |
| `--save` | 落盘到当前目录（默认只打印） |
| `--token-only` | 只检查登录态是否可用 |
| `--profile <目录>` | 指定 Firefox profile，多 profile 机器上用 |
| `--browser` | 占位参数，目前只支持 firefox，传别的会直接报错说明原因 |

## 输出示例

```
=== 会话: GTNH MV [id=284eeb7f... | 当前分支 124 条 / 全树 309 条 | 创建 2026-09-13 10:50 | 更新 2026-10-04 18:22]

--- #3 09-13 10:59
  [用户] 你是一位精通 GTNH 的陪玩助手...

--- #7 09-13 10:59
  [DeepSeek] Bro，4 个小内燃 + 2 个 LV 能源仓，硬凑 1A MV...
```

`[用户]` / `[DeepSeek]` / `[思考]` / `[搜索]` 分别对应提问、回答、思考过程、联网搜索片段。

## 关键提示

- **登录态在 localStorage，不在 cookie**：`cookies.sqlite` 里该站点只有 `smidV2` 等匿名项，令牌是 localStorage 的 `userToken`。照搬读 cookie 的写法永远拿不到登录态。
- **请求头必须与前端一致**：`x-client-version` 用 `2.5.0`、`x-client-bundle-id` 用 `com.deepseek.chat`、`x-client-timezone-offset` 用 `28800`。填错不会报错，但列表会丢 `pinned` 与 `model_type` 字段、置顶查询失效、翻页也失效，症状是"数据看着正常但少东西"，极易误判成接口限制。
- **默认只输出当前活跃分支**：网页只显示 `current_message_id` 回溯出的那条链，其余是编辑或重新生成留下的旧版本。混着输出会把用户已经改掉的说法当成真实对话内容。
- **置顶要单独查**：置顶会话不出现在 `pinned=false` 的查询结果里，脚本用两次查询合并，勿改回单次查询。
- **`model_type` 是历史遗留字段**：它记的是界面上的快速 / 专家模式（`default` / `expert` / `vision`），2026 年 9 月底模式合并后新会话恒为 `default`。它与「深度思考」是两件事，后者是消息级的 `thinking_enabled` 开关。
- **HTTP 200 不代表成功**：要同时看 `code` 与 `biz_code`，`biz_code` 非 0 时 `biz_data` 恒为 null。
- **长会话体积大**：单次返回整个会话，实测 309 条消息约 1.1 MB，渲染后数千行属正常。

更多接口细节、游标语义与实测踩坑见 `references/deepseek-web-api.md`。

## 本仓库结构

```
SKILL.md                          # 技能定义（WorkBuddy 加载）
README.md                         # 本文件
scripts/ds_read.py                # 唯一入口：列表 / 正文 / 分支 / 分享
references/deepseek-web-api.md    # 接口清单、请求头、游标语义、字段速查、踩坑记录
```

## 更新日记

- **v1.1.0**：新增分享链接读取（`--shares` / `--share <share_id>`），走 `/api/v0/share/list` 与 `/api/v0/share/content`；按 skill-creator 规范重构，把接口考古细节从 SKILL.md 移到 `references/`；纠正 `model_type` 的定性，它属历史遗留字段，与「深度思考」无关，脚本不再在会话头部把它当"模型"展示；补上识图模式（`vision`）的公开时间线。
- **v1.0.0**：初版。会话列表（含置顶分区查询与翻页）、会话正文、消息树分支渲染、思考过程过滤、原始 JSON 输出。
