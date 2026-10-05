---
name: web-ds-read
description: "读取 DeepSeek 网页版（chat.deepseek.com）的对话历史、正文与自己创建的分享链接，让 AI 能看到用户在 DeepSeek 上聊过什么。需 Firefox 已登录。Triggers: 用户说「看看我在 DeepSeek 聊了什么」「读一下 DeepSeek 的对话」「DeepSeek 上那个会话」「导出 DeepSeek 记录」「我在 DeepSeek 里问过 XX」「我分享过哪些链接」，或需要打开某个 DeepSeek 会话内容并总结、追问时使用。"
version: 1.1.0
author: WorkBuddy
tags:
  - deepseek
  - chat-history
  - conversation
  - localstorage
license: MIT
agent_created: true
---

# Web DS Read

## 用途

把 DeepSeek 网页版的对话历史读成可阅读文本，供 AI 接手总结、追问。只读，不发送消息、不删除会话、不改标题。

## 何时使用

- 用户要求查看、总结、检索自己在 DeepSeek 上的对话
- 用户给出某个 DeepSeek 会话的 id 或标题，需要读取内容
- 用户要求列出或读取自己创建的分享链接

## 前置条件

- 在本机 Windows 运行，Firefox 已登录 `chat.deepseek.com`
- 纯标准库，无需安装任何包
- 登录态取自 Firefox 新版 localStorage（`storage/default/https+++chat.deepseek.com/ls/data.sqlite` 的 `userToken` 键），不是 cookie

## 常用操作

```bash
# 先探登录态，可省掉一轮无效请求
python scripts/ds_read.py --token-only

# 列会话（置顶在前，默认 20 条，可放大到上千）
python scripts/ds_read.py --list --limit 30

# 读最近更新的会话，建议加 --no-think 过滤思考过程
python scripts/ds_read.py --latest --no-think

# 读指定会话
python scripts/ds_read.py --session <会话 id>

# 按标题关键词挑（会拉最近 200 条再筛）
python scripts/ds_read.py --keyword "MV" --no-think

# 列出自己创建的分享链接
python scripts/ds_read.py --shares

# 读取某条分享链接里的对话
python scripts/ds_read.py --share <share_id> --no-think
```

默认只打印到终端。需要存档时加 `--save`。默认只输出当前活跃分支（与网页所见一致），需要展开被编辑或重新生成换掉的旧版本时加 `--all-branches`。

## 关键约束

- **请求头必须与前端一致**：`x-client-version` 用 `2.5.0`、`x-client-bundle-id` 用 `com.deepseek.chat`、`x-client-timezone-offset` 用 `28800`。填错不报错，但列表会丢 `pinned` 与 `model_type` 字段、置顶查询失效、翻页失效。改动脚本前先读 `references/deepseek-web-api.md`。
- **只读当前活跃分支**：编辑提问或重新生成回答会在同一父节点下留下兄弟分支。默认只输出 `current_message_id` 回溯出的那条链，避免把用户已改掉的说法当成真实对话内容。
- **置顶会话需单独查询**：置顶不会出现在 `pinned=false` 的结果里，脚本已用两次查询合并，勿改回单次查询。
- **登录态只在本机读取**，令牌不外传、不写入任何输出文件。
- **HTTP 200 不代表成功**：必须同时看 `code` 与 `biz_code`。脚本已处理，改动请求逻辑时保留该判断。
- **长会话体积大**：单次返回整个会话，实测 309 条消息约 1.1 MB，渲染后数千行属正常。

## 故障排查

| 现象 | 处理 |
| --- | --- |
| 取不到登录态 | 确认 Firefox 已登录 `chat.deepseek.com` 并刷新过页面；多 profile 时用 `--profile` 指定 |
| 报 401 / 403 | token 过期，重新登录网页版后重跑，不要尝试伪造 token |
| 报 429 | 限流，隔一会儿重试，避免连续快速翻页 |
| 列表字段缺失、翻页不动 | 请求头被改错，核对 `references/deepseek-web-api.md` 第 2 节 |
| `--session` 提示 id 格式不对 | 先跑 `--list` 取 32 位 UUID，不要手填标题 |

## 打包资源

- `scripts/ds_read.py`：读取对话、列表、分享链接的唯一入口
- `references/deepseek-web-api.md`：接口清单、请求头、游标语义、字段速查与实测踩坑，排查数据异常时查阅
