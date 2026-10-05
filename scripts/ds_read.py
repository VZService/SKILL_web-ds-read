#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
web-ds-read: 读取 DeepSeek 网页版(chat.deepseek.com)的对话历史。

登录态来源: 浏览器 localStorage 的 userToken 键(不是 cookie)。
接口(从前端 bundle 实测挖出, 勿改路径):
    GET /api/v0/chat_session/fetch_page     会话列表(置顶区与普通区分开查)
    GET /api/v0/chat/history_messages       某会话的全部消息树
    GET /api/v0/share/list                  自己创建的分享链接
    GET /api/v0/share/content               某条分享链接里的对话

用法:
    python ds_read.py --list                      列出最近会话(置顶在前)
    python ds_read.py --list --limit 30           列更多, 可放大到上千条
    python ds_read.py --session <id>              读某个会话全文
    python ds_read.py --session <id> --no-think   去掉思考过程
    python ds_read.py --keyword "MV" --limit 5     按标题关键词挑会话并读全文
    python ds_read.py --latest                    最近更新的会话全文
    python ds_read.py --shares                    列出自己创建的分享链接
    python ds_read.py --share <share_id>          读某条分享链接的对话
    python ds_read.py --session <id> --all-branches  展开被编辑换掉的旧分支
    python ds_read.py --session <id> --raw        输出原始 JSON
    python ds_read.py --token-only                只打印 token 是否可用(排查登录态)

默认只打印到终端, 不落盘。--save 才写文件。
只读自己的账号数据, 不外传。
"""

import argparse
import glob
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://chat.deepseek.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:130.0) "
      "Gecko/20100101 Firefox/130.0")
# 这三个头必须跟前端 main.js 里的一致, 否则服务端会返回"精简版"数据:
# 列表项丢掉 pinned / model_type 字段, 且 pinned 查询参数整个失效。
# 取值来源: main.js 的 appConfig(brand 默认 deepseek, clientPlatform=web,
# appVersion=2.5.0) 与 HP() 映射(deepseek -> com.deepseek.chat)。
# 注意 x-client-version 是 appVersion, 不是 commit-id(44809ea4)。
CLIENT_VERSION = "2.5.0"
CLIENT_BUNDLE_ID = "com.deepseek.chat"
CLIENT_PLATFORM = "web"
# 60 * utcOffset(分钟), 东八区 = 28800 秒(正数)
CLIENT_TZ_OFFSET = "28800"
FALLBACK_DEVICE_ID = "313ac2cc-39b3-4e37-88ca-9b4de3a33dc1"


# ---------------------------------------------------------------- 浏览器登录态

def _firefox_profiles(root=None):
    """列出 Firefox 所有 profile 目录(含 cookies.sqlite 或 storage 的)。"""
    if root is None:
        if os.name == "nt":
            root = os.path.join(
                os.environ.get("APPDATA", ""), "Mozilla", "Firefox", "Profiles")
        else:
            root = os.path.expanduser("~/.mozilla/firefox")
    if not os.path.isdir(root):
        return []
    return sorted(glob.glob(os.path.join(root, "*")))


def _ls_db_path(profile, origin="https://chat.deepseek.com"):
    """Firefox 把 localStorage 存成 storage/default/<origin 转义>/ls/data.sqlite,
    转义规则是去掉 :// 换成 +++、其余 / 换成 +。"""
    esc = origin.replace("://", "+++").replace("/", "+")
    return os.path.join(profile, "storage", "default", esc, "ls", "data.sqlite")


def _read_firefox_ls(profile, origin="https://chat.deepseek.com"):
    """从 Firefox profile 的 localStorage 里读 key。

    Firefox 新版(115+)把 localStorage 从 webappsstore.sqlite 挪到了
    storage/default/<origin 转义>/ls/data.sqlite, 老的 webappsstore 是空的。
    两条路都试。
    """
    esc = origin.replace("://", "+++").replace("/", "+")
    candidates = [
        _ls_db_path(profile, origin),
        os.path.join(profile, "webappsstore.sqlite"),
    ]
    for src in candidates:
        if not os.path.exists(src):
            continue
        tmp = os.path.join(tempfile.mkdtemp(), "ls.sqlite")
        try:
            shutil.copy(src, tmp)
        except OSError:
            continue
        try:
            con = sqlite3.connect(tmp)
            rows = con.execute("SELECT key, value FROM data").fetchall()
            con.close()
        except sqlite3.Error:
            continue
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
        for k, v in rows:
            if k == "userToken":
                return _unwrap_ls_value(v)
    return None


def _unwrap_ls_value(raw):
    """Firefox 存的是 Firefox 自己的结构化序列化格式, 常见形态:
    b'{"value":"xxx","__version":"0"}'  或 b'<结构头>{"value":"xxx"}'
    这里尽力把里面的字符串抠出来。
    """
    if isinstance(raw, bytes):
        for enc in ("utf-8", "utf-16-le", "utf-16-be"):
            try:
                raw = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            raw = raw.decode("utf-8", errors="replace")
    raw = raw.strip()
    # 剥掉 Firefox 结构化序列化的头部字节(常见前导字符如 L\x90 \x93\x08 $ 等)
    start = raw.find("{")
    if start > 0:
        raw = raw[start:]
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if isinstance(obj, dict) and "value" in obj:
        v = obj["value"]
        return v if isinstance(v, str) else None
    return None


def load_token(browser="firefox", profile_dir=None):
    """取 userToken。返回 (token, 来源描述) 或 (None, 原因)。"""
    profiles = [profile_dir] if profile_dir else _firefox_profiles()
    if not profiles:
        return None, "找不到 Firefox profile 目录(是否装了 Firefox?)"
    tried = []
    for p in profiles:
        if not os.path.isdir(p):
            continue
        tok = _read_firefox_ls(p)
        if tok:
            return tok, os.path.basename(p)
        tried.append(os.path.basename(p))
    if browser != "firefox":
        return None, ("当前只实现了 Firefox 的 localStorage 读取, "
                      "你传的是 %s。DeepSeek 的 token 不在 cookie 里, "
                      "Edge/Chrome 又是 App-Bound 加密, 一律读不到。" % browser)
    return None, ("以下 profile 里没找到 userToken: %s。"
                  "请先在 Firefox 里登录 chat.deepseek.com 并刷新页面。"
                  % ", ".join(tried) or "无")


def load_device_id(profile_dir=None):
    """取本机 device id, 缺失时用固定回退值(接口不强校验)。"""
    profiles = [profile_dir] if profile_dir else _firefox_profiles()
    for p in profiles:
        src = _ls_db_path(p)
        if not os.path.exists(src):
            continue
        tmp = os.path.join(tempfile.mkdtemp(), "ls2.sqlite")
        try:
            shutil.copy(src, tmp)
            con = sqlite3.connect(tmp)
            rows = con.execute("SELECT key, value FROM data").fetchall()
            con.close()
        except (OSError, sqlite3.Error):
            continue
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
        for k, v in rows:
            if k == "deepseek-device-id:chat":
                if isinstance(v, bytes):
                    v = v.decode("utf-8", errors="replace")
                if isinstance(v, str) and v.strip():
                    return v.strip()
    return FALLBACK_DEVICE_ID


# ---------------------------------------------------------------- HTTP

def build_headers(token, device_id):
    return {
        "User-Agent": UA,
        "Accept": "application/json",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Authorization": "Bearer " + token,
        "x-client-platform": CLIENT_PLATFORM,
        "x-client-locale": "zh_CN",
        "x-client-version": CLIENT_VERSION,
        "x-client-bundle-id": CLIENT_BUNDLE_ID,
        "x-client-timezone-offset": CLIENT_TZ_OFFSET,
        "x-device-id": device_id,
        "Referer": BASE + "/",
        "Origin": BASE,
    }


def api_get(path, token, device_id, query=None, timeout=40):
    url = BASE + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    req = urllib.request.Request(url, headers=build_headers(token, device_id))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:300]
        if e.code in (401, 403):
            raise RuntimeError("鉴权被拒(HTTP %d), token 可能已过期, "
                               "去 Firefox 里重新登录一次 DeepSeek 即可。" % e.code)
        if e.code == 429:
            raise RuntimeError("被限流(HTTP 429), 隔一会儿再试。")
        raise RuntimeError("HTTP %d: %s" % (e.code, detail))
    except urllib.error.URLError as e:
        raise RuntimeError("网络不通: %s" % e)
    try:
        data = json.loads(body)
    except ValueError:
        raise RuntimeError("返回不是 JSON(可能被风控挡了), 前 200 字: %s" % body[:200])
    if data.get("code") != 0:
        raise RuntimeError("接口报错 code=%s msg=%s" % (data.get("code"), data.get("msg")))
    inner = data.get("data") or {}
    biz_code = inner.get("biz_code")
    biz = inner.get("biz_data")
    # 关键: HTTP 200 不代表成功。biz_code 非 0 时 biz_data 恒为 null,
    # 典型是 fetch_page 的 count 太小(实测 count=1 -> ILLEGAL_COUNT)。
    # 早先只看 code 会把这种失败当成"账号里没有会话"。
    if biz_code not in (0, None):
        raise RuntimeError("接口业务错误 biz_code=%s biz_msg=%s"
                           % (biz_code, inner.get("biz_msg")))
    return biz or {}


def fetch_sessions(token, device_id, count=100, include_pinned=True):
    """拉会话列表(含置顶)。

    分区查询, 实测服务端只在 gte 方向认 pinned 过滤:
      置顶区 = gte_cursor.pinned=true  & updated_at=0        (一次拿全)
      普通区 = lte_cursor.pinned=false & updated_at=<未来大数>, 之后用本页末条
               updated_at 当锚点继续往回翻
    早先用错请求头时, pinned 参数被整个忽略、列表也拿不到 pinned 字段,
    所以曾误判成"接口只给 50 条且游标无效"。
    """
    count = max(2, min(1000, count)) if count else 1000
    out, seen = [], set()

    def add(batch):
        n = 0
        for s in batch:
            sid = s.get("id")
            if sid and sid not in seen:
                seen.add(sid)
                out.append(s)
                n += 1
        return n

    if include_pinned:
        biz = api_get("/api/v0/chat_session/fetch_page", token, device_id, {
            "gte_cursor.pinned": "true",
            "gte_cursor.updated_at": "0",
            "count": 50,
        })
        add(biz.get("chat_sessions") or [])

    anchor = "9999999999"   # 未来时间戳, 表示"从最新开始往回"
    while len(out) < count:
        biz = api_get("/api/v0/chat_session/fetch_page", token, device_id, {
            "lte_cursor.pinned": "false",
            "lte_cursor.updated_at": anchor,
            "count": 50,
        })
        batch = biz.get("chat_sessions") or []
        if not batch:
            break
        added = add(batch)
        if not biz.get("has_more") or added == 0:
            break
        anchor = repr(batch[-1].get("updated_at"))
        if len(out) >= count:
            break
    return out[:count]


def fetch_messages(session_id, token, device_id):
    """拉单个会话的全部消息。cache_version / cache_reset_at 不能传空串
    (会400: cannot parse integer from empty string), 不传反而正常。"""
    biz = api_get("/api/v0/chat/history_messages", token, device_id,
                  {"chat_session_id": session_id})
    return biz.get("chat_session") or {}, biz.get("chat_messages") or []


def fetch_share_content(share_id, token, device_id):
    """读取某条分享链接里的对话。结构与 history_messages 同构:
    biz_data = {title, messages[], model_type}, 但没有 current_message_id。"""
    biz = api_get("/api/v0/share/content", token, device_id,
                  {"share_id": share_id})
    return biz.get("title") or "来自分享的对话", biz.get("messages") or []


def fetch_shares(token, device_id, count=100):
    """列出账号下已创建的分享链接。

    count 是必填参数, 少了会回 422 (detail: query.count)。
    返回项: share_id / hint(标题) / created_at / chat_session_id。
    """
    out, seen, offset = [], set(), 0
    count = max(1, count)
    while len(out) < count:
        biz = api_get("/api/v0/share/list", token, device_id,
                      {"count": 50, "offset": offset})
        batch = biz.get("shares") or []
        if not batch:
            break
        for s in batch:
            sid = s.get("share_id")
            if sid and sid not in seen:
                seen.add(sid)
                out.append(s)
        if not biz.get("has_more"):
            break
        offset += len(batch)
    return out[:count]


# ---------------------------------------------------------------- 渲染

FRAG_LABEL = {
    "REQUEST": "用户",
    "RESPONSE": "DeepSeek",
    "THINK": "思考",
    "SEARCH": "搜索",
}


def render_fragments(msg, no_think=False):
    out = []
    for fr in msg.get("fragments") or []:
        ftype = fr.get("type", "")
        content = (fr.get("content") or "").strip()
        if not content:
            continue
        if no_think and ftype == "THINK":
            continue
        label = FRAG_LABEL.get(ftype, ftype or "内容")
        out.append("  [%s] %s" % (label, content))
    return out


def _active_chain(session, by_id):
    """沿 current_message_id 回溯, 得到网页上当前显示的那条分支链。

    DeepSeek 编辑提问、重新生成回答都会产生兄弟分支, 服务端把整棵树都返回。
    网页只显示 current_message_id 这一条, 混着渲染会把废弃版本一起吐出来。
    """
    cur = session.get("current_message_id")
    chain = []
    guard = 0
    while cur is not None and cur in by_id and guard < 20000:
        chain.append(cur)
        cur = by_id[cur].get("parent_id")
        guard += 1
    chain.reverse()
    return chain


def render_session(session, messages, no_think=False, show_id=False,
                   all_branches=False):
    lines = []
    by_id = {m.get("message_id"): m for m in messages}
    children = {}
    for m in messages:
        children.setdefault(m.get("parent_id"), []).append(m)
    roots = children.get(None) or []
    if not roots:
        # 分享内容等场景: 消息树是被截取的一段, 顶层消息的 parent_id 指向
        # 不在返回集合里的节点, 此时把"父不在集合内"的当作根。
        roots = [m for m in messages if m.get("parent_id") not in by_id]
        roots.sort(key=lambda m: m.get("message_id") or 0)

    chain = _active_chain(session, by_id)
    chain_set = set(chain)

    head = "会话: %s" % (session.get("title") or "(无标题)")
    meta = []
    if show_id:
        meta.append("id=%s" % session.get("id"))
    if chain and len(chain) < len(messages):
        meta.append("当前分支 %d 条 / 全树 %d 条" % (len(chain), len(messages)))
    else:
        meta.append("消息 %d 条" % len(messages))
    # model_type 是历史遗留字段(2026 年 9 月底起模式已合并, 新会话恒为 default),
    # 当前不再有区分度, 默认不展示, 需要时用 --raw 取原值。
    for label, key in (("创建", "inserted_at"), ("更新", "updated_at")):
        ts = session.get(key)
        if ts:
            meta.append("%s %s" % (label, time.strftime("%Y-%m-%d %H:%M",
                                                        time.localtime(ts))))
    if all_branches:
        meta.append("模式 全部分支")
    lines.append("=== %s [%s]" % (head, " | ".join(meta)))

    if not roots:
        lines.append("(消息树无根节点, 可能是空会话)")
        return lines

    def emit(m, tag=""):
        stamp = time.strftime("%m-%d %H:%M",
                              time.localtime(m.get("inserted_at") or 0))
        body = render_fragments(m, no_think)
        lines.append("")
        lines.append("--- #%s %s%s" % (m.get("message_id"), stamp, tag))
        lines.extend(body or ["  (无正文)"])

    # 默认: 只走当前活跃链, 跟网页上看到的一致
    if not all_branches and chain:
        for mid in chain:
            m = by_id.get(mid)
            if m is not None:
                emit(m)
        hidden = len(messages) - len(chain)
        if hidden:
            lines.append("")
            lines.append("(另有 %d 条消息落在被编辑或重新生成换掉的旧分支上, "
                         "加 --all-branches 可全部展开)" % hidden)
        return lines

    # 全部分支: DFS 展开整棵树, 分支点标注序号与是否当前分支
    emitted = set()

    def walk(mid):
        if mid in emitted:
            return
        emitted.add(mid)
        m = by_id.get(mid)
        if m is None:
            return
        sibs = children.get(m.get("parent_id")) or []
        tag = ""
        if len(sibs) > 1:
            order = [s.get("message_id") for s in sibs]
            tag = "  [分支 %d/%d%s]" % (order.index(mid) + 1, len(sibs),
                                        ", 当前" if mid in chain_set else "")
        emit(m, tag)
        for k in children.get(mid) or []:
            walk(k.get("message_id"))

    for r in roots:
        walk(r.get("message_id"))
    return lines


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(
        description="读取 DeepSeek 网页版对话历史(只读, 需 Firefox 已登录)")
    ap.add_argument("--list", action="store_true", help="列出会话")
    ap.add_argument("--session", help="会话 id")
    ap.add_argument("--latest", action="store_true", help="最近一个会话")
    ap.add_argument("--shares", action="store_true", help="列出自己创建的分享链接")
    ap.add_argument("--share", help="读取某条分享链接的对话(share_id)")
    ap.add_argument("--keyword", help="按标题关键词筛会话并读全文")
    ap.add_argument("--limit", type=int, default=20, help="列表条数/关键词命中上限")
    ap.add_argument("--browser", default="firefox", help="读哪个浏览器的登录态")
    ap.add_argument("--profile", help="指定 Firefox profile 目录")
    ap.add_argument("--no-think", action="store_true", help="不输出思考过程")
    ap.add_argument("--all-branches", action="store_true",
                    help="展开全部历史分支(含被编辑/重新生成换掉的旧版本)。"
                         "默认只输出当前活跃分支, 与网页所见一致")
    ap.add_argument("--raw", action="store_true", help="输出原始 JSON")
    ap.add_argument("--save", action="store_true", help="落盘保存")
    ap.add_argument("--token-only", action="store_true", help="只检查登录态")
    args = ap.parse_args()

    token, source = load_token(args.browser, args.profile)
    if not token:
        print("[X] 取不到 DeepSeek 登录态: %s" % source)
        sys.exit(2)
    device_id = load_device_id(args.profile)

    if args.token_only:
        print("[+] 登录态可用 (profile: %s, token %d 位)" % (source, len(token)))
        return

    if not (args.list or args.session or args.latest or args.keyword
            or args.shares or args.share):
        ap.print_help()
        return

    try:
        if args.shares:
            shares = fetch_shares(token, device_id, max(1, args.limit))
            if args.raw:
                print(json.dumps(shares, ensure_ascii=False, indent=2))
                return
            print("[+] 分享链接(共 %d 条):" % len(shares))
            for i, s in enumerate(shares, 1):
                ts = time.strftime("%Y-%m-%d %H:%M",
                                   time.localtime(s.get("created_at") or 0))
                print("  %2d. %s  %s  [%s]" % (
                    i, ts, s.get("hint") or "(无标题)", s.get("share_id")))
                if s.get("chat_session_id"):
                    print("      源会话 %s" % s["chat_session_id"])
            return

        if args.share:
            title, msgs = fetch_share_content(args.share, token, device_id)
            if args.raw:
                print(json.dumps({"title": title, "messages": msgs},
                                 ensure_ascii=False, indent=2))
                return
            lines = render_session({"title": title}, msgs, args.no_think,
                                   all_branches=True)
            print("\n".join(lines))
            if args.save:
                _save("\n".join(lines), "deepseek-share-%s.txt" % args.share)
            return

        if args.list:
            sessions = fetch_sessions(token, device_id, max(1, args.limit))
            if args.raw:
                print(json.dumps(sessions, ensure_ascii=False, indent=2))
                return
            pinned_n = sum(1 for s in sessions if s.get("pinned"))
            print("[+] 会话列表(共 %d 条, 置顶 %d 条在前, 其余按更新时间倒序):"
                  % (len(sessions), pinned_n))
            for i, s in enumerate(sessions, 1):
                ts = time.strftime("%Y-%m-%d %H:%M",
                                  time.localtime(s.get("updated_at") or 0))
                title = s.get("title") or "(无标题)"
                pin = " [置顶]" if s.get("pinned") else ""
                print("  %2d. %s  %s%s  [%s]" % (i, ts, title, pin, s.get("id")))
            return

        if args.keyword:
            sessions = fetch_sessions(token, device_id, max(args.limit * 5, 200))
            hits = [s for s in sessions
                    if args.keyword in (s.get("title") or "")]
            if not hits:
                print("[X] 没找到标题含 %r 的会话(最近 %d 条里找的)"
                      % (args.keyword, len(sessions)))
                sys.exit(1)
            targets = hits[:args.limit]
            print("[+] 命中 %d 个会话:" % len(targets))
            for s in targets:
                print("  - %s [%s]" % (s.get("title"), s.get("id")))
            out = []
            for s in targets:
                sess, msgs = fetch_messages(s["id"], token, device_id)
                out.extend(render_session(sess or s, msgs, args.no_think,
                                          all_branches=args.all_branches))
                out.append("")
            print("\n".join(out))
            if args.save:
                _save("\n".join(out), "deepseek-keyword.txt")
            return

        if args.latest:
            # 列表现在置顶在前, 不能直接取第 1 条, 要按 updated_at 取最新
            sessions = fetch_sessions(token, device_id, 100)
            if not sessions:
                print("[X] 账号里没有会话")
                sys.exit(1)
            target = max(sessions, key=lambda s: s.get("updated_at") or 0)
        else:
            # 服务端对非法 UUID 直接回 400 且报文是 Rust 风格错误, 先本地拦一道
            if not re.fullmatch(r"[0-9a-fA-F-]{36}", args.session or ""):
                print("[X] --session 要填会话 id(32 位十六进制 UUID), "
                      "先用 --list 看列表。收到: %r" % args.session)
                sys.exit(1)
            target = {"id": args.session, "title": "(按 id 指定)"}

        sess, msgs = fetch_messages(target["id"], token, device_id)
        if not sess and not msgs:
            print("[X] 会话 %s 取不到内容(可能已删除, 或 id 写错)" % target["id"])
            sys.exit(1)
        if args.raw:
            print(json.dumps({"chat_session": sess, "chat_messages": msgs},
                             ensure_ascii=False, indent=2))
            return
        lines = render_session(sess or target, msgs, args.no_think,
                               show_id=not args.latest,
                               all_branches=args.all_branches)
        print("\n".join(lines))
        if args.save:
            name = (sess.get("title") or "session").replace("/", "_")[:40]
            _save("\n".join(lines), "deepseek-%s.txt" % name)

    except RuntimeError as e:
        print("[X] %s" % e)
        sys.exit(1)


def _save(text, filename):
    path = os.path.join(os.getcwd(), filename)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text + "\n")
    print("[+] 已写入: %s" % path)


if __name__ == "__main__":
    main()
