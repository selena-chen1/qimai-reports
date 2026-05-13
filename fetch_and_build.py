#!/usr/bin/env python3
"""
七麦数据 6 款休闲游戏周报生成器

工作原理：
  1. 通过 Chrome DevTools Protocol 连接到 127.0.0.1:9222 上已登录的 Chrome
  2. 在已登录页面里执行 fetch() 调用七麦 API（Cookie 由浏览器自动带上，脚本不接触）
  3. 生成本周 HTML 快照 (2026-wXX.html) + 更新列表页 (index.html)

前置：
  Chrome 必须用 --remote-debugging-port=9222 启动，且已登录七麦

用法：
  python3 fetch_and_build.py
"""

import json, re, sys, time, urllib.request, base64
from datetime import datetime, date, timedelta
from pathlib import Path

# ============================================================
# 配置
# ============================================================
GAMES = [
    {"name": "地铁跑酷",        "id": "995122577"},
    {"name": "神庙逃亡2",       "id": "1014227673"},
    {"name": "我的世界",        "id": "1243986797"},
    {"name": "迷你世界",        "id": "1170455562"},
    {"name": "贪吃蛇大作战",     "id": "1120536875"},
    {"name": "植物大战僵尸2",    "id": "639516529"},
]
REPORTS_DIR = Path(__file__).parent
CDP_URL = "http://127.0.0.1:9222"

# ============================================================
# CDP: 找一个 qimai 标签页，发送 fetch 调用并拿到结果
# ============================================================
def find_qimai_tab():
    try:
        with urllib.request.urlopen(f"{CDP_URL}/json", timeout=3) as r:
            tabs = json.loads(r.read())
    except Exception as e:
        print(f"❌ 无法连接到 Chrome 调试端口 {CDP_URL}")
        print(f"   请先用 --remote-debugging-port=9222 启动 Chrome 并登录七麦")
        print(f"   错误：{e}")
        sys.exit(1)
    # Prefer an active qimai tab; otherwise any qimai tab
    qimai_tabs = [t for t in tabs if t.get("type") == "page" and "qimai.cn" in (t.get("url") or "")]
    if not qimai_tabs:
        print("❌ 当前 Chrome 没有打开任何七麦页面")
        print("   请先在浏览器里打开 https://www.qimai.cn/ 并确认已登录")
        sys.exit(1)
    return qimai_tabs[0]

def cdp_fetch(tab, url):
    """通过 CDP 在标签页里执行 fetch，返回 JSON 响应"""
    import websocket  # type: ignore
    ws_url = tab["webSocketDebuggerUrl"]
    # Chrome 111+ blocks WS connections with foreign Origin header.
    # suppress_origin=True 让 websocket-client 不发 Origin，Chrome 默认接受这种情况。
    ws = websocket.create_connection(ws_url, timeout=15, suppress_origin=True)
    try:
        expr = f"""(async () => {{
            try {{
                const r = await fetch({json.dumps(url)}, {{ credentials: 'include', headers: {{ 'accept':'application/json' }} }});
                return JSON.stringify(await r.json());
            }} catch (e) {{ return JSON.stringify({{__err: String(e)}}); }}
        }})()"""
        ws.send(json.dumps({
            "id": 1,
            "method": "Runtime.evaluate",
            "params": {"expression": expr, "awaitPromise": True, "returnByValue": True}
        }))
        resp = json.loads(ws.recv())
        result = resp.get("result", {}).get("result", {})
        val = result.get("value")
        if val is None:
            return {"__err": "no value", "raw": resp}
        return json.loads(val)
    finally:
        ws.close()

# ============================================================
# 抓数据
# ============================================================
def fetch_all_data(tab):
    today = date.today()
    sdate = (today - timedelta(days=90)).isoformat()
    edate = today.isoformat()
    out = {}
    for g in GAMES:
        print(f"  📥 抓取 {g['name']} ...")
        rec = {"id": g["id"]}
        rec["free"] = cdp_fetch(tab, f"https://api.qimai.cn/app/rankMore?appid={g['id']}&country=cn&export_type=app_rank&brand=free&day=1&appRankShow=1&subclass=all&simple=1&sdate={sdate}&edate={edate}&rankEchartType=1")
        rec["rt"] = cdp_fetch(tab, f"https://api.qimai.cn/app/rank?appid={g['id']}&country=cn&export_type=app_rank&brand=free")
        rec["version"] = cdp_fetch(tab, f"https://api.qimai.cn/app/version?appid={g['id']}&country=cn")
        rec["featured"] = cdp_fetch(tab, f"https://api.qimai.cn/app/featured?appid={g['id']}&country=cn")
        out[g["name"]] = rec
        time.sleep(0.3)
    return {"window": {"sdate": sdate, "edate": edate}, "raw": out}

# ============================================================
# 数据处理
# ============================================================
def parse_cn_date(s):
    m = re.match(r"(\d{4})年(\d{2})月(\d{2})日", s or "")
    return date(int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None

def avg_rank_in_range(daily, d_from, d_to):
    vals = [v for k, v in daily.items() if d_from <= k <= d_to and v is not None]
    return round(sum(vals) / len(vals)) if vals else None

def build_processed(raw_dataset):
    window = raw_dataset["window"]
    sdate_d = date.fromisoformat(window["sdate"])
    edate_d = date.fromisoformat(window["edate"])

    # 12 Mondays ending on/before edate_d
    monday = edate_d - timedelta(days=edate_d.weekday())
    weeks = [(monday - timedelta(weeks=11 - i)).isoformat() for i in range(12)]

    processed = {"generated_at": datetime.now().isoformat(), "window": window, "weeks": weeks, "apps": {}}

    for g in GAMES:
        name = g["name"]
        r = raw_dataset["raw"][name]
        if r["free"].get("code") != 10000:
            print(f"  ⚠ {name} free 接口异常：{r['free'].get('msg')}")
            continue

        table = r["free"].get("data", {}).get("table", [])
        # daily lookup
        daily = {}
        free_trend = []
        for row in table:
            d = datetime.fromtimestamp(row["date"] / 1000).date()
            g_rank = row.get("6014")
            g_rank = None if g_rank == "-" else g_rank
            daily[d] = g_rank
            free_trend.append({"d": row["date"], "g": g_rank})

        # weekly averages
        weekly = []
        for w_iso in weeks:
            w_start = date.fromisoformat(w_iso)
            w_end = w_start + timedelta(days=6)
            weekly.append(avg_rank_in_range(daily, w_start, w_end))

        # realtime
        rt = r["rt"].get("realTimeRank") or []
        rt_free = rt[1] if len(rt) > 1 else []
        rt_pay = rt[2] if len(rt) > 2 else []
        realtime = {
            "fg": (rt_free[1] or {}).get("ranking") if len(rt_free) > 1 else None,
            "ft": (rt_free[4] or {}).get("ranking") if len(rt_free) > 4 else None,
            "pg": (rt_pay[1] or {}).get("ranking") if len(rt_pay) > 1 else None,
            "pt": (rt_pay[4] or {}).get("ranking") if len(rt_pay) > 4 else None,
        }

        # versions in window
        vlist_raw = r["version"].get("version", []) if r["version"].get("code") == 10000 else []
        versions = []
        for v in vlist_raw:
            vd = parse_cn_date(v.get("release_time"))
            if not vd or vd < sdate_d:
                continue
            before = avg_rank_in_range(daily, vd - timedelta(days=7), vd - timedelta(days=1))
            after = avg_rank_in_range(daily, vd + timedelta(days=1), vd + timedelta(days=7))
            note = (v.get("release_note") or "").replace("<br />", "\n").replace("<br/>", "\n").replace("<br>", "\n")
            note = re.sub(r"&[^;]+;", " ", note).strip()
            versions.append({
                "v": v.get("version"),
                "d": v.get("release_time"),
                "s": v.get("subtitle"),
                "n": note[:300],
                "before": before,
                "after": after,
            })
        versions.sort(key=lambda x: parse_cn_date(x["d"]) or date(2000, 1, 1), reverse=True)

        # featured: 精品推荐位
        featured_raw = r.get("featured", {}).get("featured", []) if r.get("featured", {}).get("code") == 10000 else []
        featured_in_window = []
        for f in featured_raw:
            sdate_str = f.get("sdate") or ""
            if not sdate_str:
                continue
            try:
                fd = datetime.strptime(sdate_str.split(" ")[0], "%Y-%m-%d").date()
            except Exception:
                continue
            if fd < sdate_d:
                continue
            featured_in_window.append({
                "name": f.get("name") or "",
                "genre": f.get("genre") or "",
                "rank": f.get("rank") or "—",
                "sdate": sdate_str,
                "edate": f.get("edate") or "至今",
                "duration": f.get("duration") or "",
                "sort_sdate": f.get("sort_sdate", 0),
            })
        featured_in_window.sort(key=lambda x: x.get("sort_sdate", 0), reverse=True)

        # 版更前后推荐位增减统计
        def featured_count_in_range(d_from, d_to):
            return sum(1 for f in featured_in_window
                       if f["sort_sdate"]
                       and d_from <= datetime.fromtimestamp(f["sort_sdate"]).date() <= d_to)

        if versions:
            b7_total = a14_total = 0
            for v in versions:
                vd = parse_cn_date(v["d"])
                if not vd: continue
                b7_total += featured_count_in_range(vd - timedelta(days=7), vd - timedelta(days=1))
                a14_total += featured_count_in_range(vd, vd + timedelta(days=14))
            n_v = len(versions)
            avg_before = round(b7_total / n_v, 1)
            avg_after = round(a14_total / n_v, 1)
            ratio = round(avg_after / avg_before, 2) if avg_before > 0 else None
        else:
            avg_before = avg_after = ratio = None

        featured_stats = {
            "count": len(featured_in_window),
            "avg_before_7d": avg_before,
            "avg_after_14d": avg_after,
            "ratio": ratio,
        }

        # icon: try to find in response
        icon = ""
        for v in vlist_raw[:1]:
            icon = v.get("icon", "")

        processed["apps"][name] = {
            "id": g["id"],
            "icon": icon,
            "free_trend": free_trend,
            "weekly": weekly,
            "realtime": realtime,
            "versions": versions,
            "featured": featured_in_window,
            "featured_stats": featured_stats,
        }

    return processed

# ============================================================
# 图标内联（base64）—— 解决飞书等平台无法加载七麦 CDN 图片的问题
# ============================================================
def inline_icon(url):
    if not url:
        return ""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = r.read()
            ct = r.headers.get_content_type() or "image/png"
        return f"data:{ct};base64,{base64.b64encode(data).decode()}"
    except Exception as e:
        print(f"  ⚠ 图标内联失败 {url[:60]}: {e}")
        return url  # 失败时回退到原 URL

# ============================================================
# HTML 生成
# ============================================================
def render_html(data, week_id):
    weeks_iso = data["weeks"]
    def week_label(iso):
        d0 = date.fromisoformat(iso); d1 = d0 + timedelta(days=6)
        return f"{d0.month}/{d0.day}–{d1.month}/{d1.day}"
    week_labels = [week_label(w) for w in weeks_iso]

    games = [g["name"] for g in GAMES]

    # Inline icons
    print("  🖼  内联图标为 base64...")
    for n in games:
        app = data["apps"].get(n)
        if app:
            app["icon_inline"] = inline_icon(app.get("icon"))

    # ===== ① 整体趋势速览 =====
    def fmt_delta(curr, prev):
        if curr is None or prev is None: return '<span class="neutral">—</span>'
        d = prev - curr
        if d == 0: return '<span class="neutral">持平</span>'
        if d > 0:  return f'<span class="up">↑ {d} 名</span>'
        return f'<span class="down">↓ {-d} 名</span>'

    summary_rows = ""
    for n in games:
        app = data["apps"].get(n)
        if not app: continue
        weekly = app["weekly"]
        valid = [v for v in weekly if v is not None]
        first, last = weekly[0], weekly[-1]
        prev_week = weekly[-2] if len(weekly) >= 2 else None  # 上周
        # 本周 vs 上周
        if last is None or prev_week is None:
            wow_html = '<span class="neutral">—</span>'
        else:
            wow = prev_week - last
            if wow > 0:   wow_html = f'<span class="up">↑ {wow} 名</span>'
            elif wow < 0: wow_html = f'<span class="down">↓ {-wow} 名</span>'
            else:         wow_html = '<span class="neutral">持平</span>'
        mn = min(valid) if valid else None
        mx = max(valid) if valid else None
        ch = (first - last) if (first is not None and last is not None) else None
        if ch is None: ch_html = '<span class="neutral">—</span>'
        elif ch > 0:   ch_html = f'<span class="up">↑ {ch} 名</span>'
        elif ch < 0:   ch_html = f'<span class="down">↓ {-ch} 名</span>'
        else:          ch_html = '<span class="neutral">持平</span>'
        rt = app["realtime"]
        n_versions = len(app["versions"])
        n_featured = (app.get("featured_stats") or {}).get("count") or 0
        summary_rows += f"""<tr>
          <td><div class="game-cell"><img src="{app['icon_inline']}" /><span>{n}</span></div></td>
          <td class="num"><b style="color:#2563eb;">#{rt['fg'] or '—'}</b></td>
          <td class="num"><b>#{last or '—'}</b></td>
          <td class="num">#{prev_week or '—'}</td>
          <td class="num">{wow_html}</td>
          <td class="num">{('#'+str(rt['pg'])) if rt['pg'] and rt['pg'] != '-' else '—'}</td>
          <td class="num">#{first or '—'}</td>
          <td class="num">#{mn or '—'}</td>
          <td class="num">#{mx or '—'}</td>
          <td class="num">{ch_html}</td>
          <td class="num">{n_versions}</td>
          <td class="num">{n_featured}</td>
        </tr>"""

    # ===== ② 周维度排名表 =====
    def week_cell(curr, prev):
        if curr is None: return '<td class="empty">—</td>'
        if prev is None:
            return f'<td><div class="rank">#{curr}</div><div class="delta neutral">—</div></td>'
        d = prev - curr
        if d == 0: return f'<td><div class="rank">#{curr}</div><div class="delta neutral">持平</div></td>'
        if d > 0:  return f'<td><div class="rank">#{curr}</div><div class="delta up">↑ {d} 名</div></td>'
        return f'<td><div class="rank">#{curr}</div><div class="delta down">↓ {-d} 名</div></td>'

    rank_rows = ""
    for n in games:
        app = data["apps"].get(n)
        if not app: continue
        w = app["weekly"]
        # 逆序：最新的周放最左边；每格的"较上周变动"对比的是右边那一格（更早的一周）
        w_rev = list(reversed(w))
        cells = "".join(
            week_cell(r, w_rev[i+1] if i + 1 < len(w_rev) else None)
            for i, r in enumerate(w_rev)
        )
        rank_rows += f"""<tr>
          <th class="sticky-col"><div class="game-cell"><img src="{app['icon_inline']}" /><span>{n}</span></div></th>
          {cells}
        </tr>"""

    # ===== ③ 版本更新明细 =====
    version_blocks = ""
    for n in games:
        app = data["apps"].get(n)
        if not app: continue
        vlist = app["versions"]
        if not vlist:
            version_blocks += f"""
            <div class="version-group">
              <div class="version-group-head">
                <img src="{app['icon_inline']}" /><h3>{n}</h3><span class="badge-empty">窗口期内未发布新版本</span>
              </div>
            </div>"""
            continue
        rows = ""
        for v in vlist:
            before, after = v["before"], v["after"]
            if before is not None and after is not None:
                d = before - after
                if d > 0:   delta_html = f'<span class="up">↑ {d} 名</span>'
                elif d < 0: delta_html = f'<span class="down">↓ {-d} 名</span>'
                else:       delta_html = '<span class="neutral">持平</span>'
                move = f'#{before} → #{after} <span class="move">{delta_html}</span>'
            elif after is not None:
                move = f"前周无数据 → #{after}"
            else:
                move = '<span class="neutral">无数据</span>'
            note = (v.get("n") or "").replace("\n", "  ").strip()[:160]
            if v.get("n") and len(v["n"]) > 160: note += "..."
            rows += f"""<tr>
              <td class="mono">v{v['v']}</td>
              <td class="nowrap">{v['d']}</td>
              <td><b>{v.get('s') or ''}</b><br/><span class="muted">{note}</span></td>
              <td class="nowrap">{move}</td>
            </tr>"""
        version_blocks += f"""
        <div class="version-group">
          <div class="version-group-head">
            <img src="{app['icon_inline']}" /><h3>{n}</h3><span class="badge-count">{len(vlist)} 个版本</span>
          </div>
          <div class="table-wrap">
            <table class="version-table">
              <thead><tr><th>版本号</th><th>发布日期</th><th>更新摘要</th><th>前后周排名变动</th></tr></thead>
              <tbody>{rows}</tbody>
            </table>
          </div>
        </div>"""

    # ===== ④ 精品推荐位：概览表 + 明细下拉 =====
    feat_summary_rows = ""
    for n in games:
        app = data["apps"].get(n)
        if not app: continue
        fs = app.get("featured_stats") or {}
        nv = len(app.get("versions", []))
        cnt = fs.get("count") or 0
        b7 = fs.get("avg_before_7d")
        a14 = fs.get("avg_after_14d")
        ratio = fs.get("ratio")

        if ratio is None:
            verdict = '<span class="neutral">N/A（无版本）</span>'
        elif ratio >= 2:
            verdict = '<span class="up">📈 明显增多</span>'
        elif ratio >= 1.3:
            verdict = '<span class="up" style="opacity:.75;">略有增多</span>'
        elif ratio >= 0.8:
            verdict = '<span class="neutral">基本持平</span>'
        else:
            verdict = '<span class="down">略减少</span>'

        feat_summary_rows += f"""<tr>
          <td><div class="game-cell"><img src="{app['icon_inline']}" /><span>{n}</span></div></td>
          <td class="num">{nv}</td>
          <td class="num">{cnt}</td>
          <td class="num">{b7 if b7 is not None else '—'}</td>
          <td class="num">{a14 if a14 is not None else '—'}</td>
          <td class="num">{ratio if ratio is not None else '—'}</td>
          <td>{verdict}</td>
        </tr>"""

    featured_details = ""
    for n in games:
        app = data["apps"].get(n)
        if not app: continue
        feats = app.get("featured", [])
        if not feats:
            rows_html = '<tr><td colspan="6" class="muted" style="text-align:center;">窗口期内无新增推荐位</td></tr>'
        else:
            rows_html = ""
            for f in feats:
                rows_html += f"""<tr>
                  <td class="nowrap">{f['sdate']}</td>
                  <td><b>{f['name']}</b></td>
                  <td class="muted">{f['genre']}</td>
                  <td class="num">#{f['rank']}</td>
                  <td class="muted nowrap">{f['edate']}</td>
                  <td class="muted nowrap">{f['duration']}</td>
                </tr>"""
        featured_details += f"""
        <details class="detail-block">
          <summary>
            <img src="{app['icon_inline']}" />
            <span class="dname">{n}</span>
            <span class="dcount">{len(feats)} 条新增推荐</span>
            <span class="darrow">▼</span>
          </summary>
          <div class="table-wrap">
            <table>
              <thead><tr><th>加入日期</th><th>推荐位名称</th><th>所在分类</th><th>当时排名</th><th>下榜日期</th><th>已上时长</th></tr></thead>
              <tbody>{rows_html}</tbody>
            </table>
          </div>
        </details>"""

    # ===== ⑤ 关键洞察（动态生成核心结论）=====
    # 找最大下滑 / 最稳定 / 进入畅销榜的
    snapshot = {}
    for n in games:
        app = data["apps"].get(n)
        if not app: continue
        weekly = app["weekly"]
        valid = [v for v in weekly if v is not None]
        if not valid: continue
        snapshot[n] = {
            "first": weekly[0], "last": weekly[-1],
            "min": min(valid), "max": max(valid),
            "range": max(valid) - min(valid),
            "drop": (weekly[0] - weekly[-1]) if (weekly[0] and weekly[-1]) else 0,  # 正=提升 负=下滑
            "n_versions": len(app["versions"]),
            "pg": app["realtime"].get("pg"),
            "pt": app["realtime"].get("pt"),
        }

    biggest_dropper = min(snapshot.items(), key=lambda x: x[1]["drop"]) if snapshot else None
    most_stable = sorted(snapshot.items(), key=lambda x: x[1]["range"])[:2]
    in_paid_chart = [(n, s["pg"]) for n, s in snapshot.items() if s["pg"] and s["pg"] != "-" and isinstance(s["pg"], int) and s["pg"] <= 200]

    # Find biggest single-week jump
    biggest_jump = None
    for n, app in data["apps"].items():
        w = app["weekly"]
        for i in range(1, len(w)):
            if w[i-1] is not None and w[i] is not None:
                jump = w[i-1] - w[i]
                if biggest_jump is None or jump > biggest_jump["jump"]:
                    biggest_jump = {"name": n, "jump": jump, "from": w[i-1], "to": w[i], "week": week_labels[i]}

    insights = ""
    if biggest_dropper and biggest_dropper[1]["drop"] < 0:
        n, s = biggest_dropper
        insights += f"""<div class="insight"><b>🚨 {n} 持续下滑</b> — 12 周从 #{s['first']} 跌到 #{s['last']}（绿色 ↓{-s['drop']} 名），窗口期 <b>{s['n_versions']} 个版本更新</b>。</div>"""

    if most_stable and in_paid_chart:
        # 排除地铁跑酷（单独成一条洞察）
        paid_pure = [(n, pg) for n, pg in in_paid_chart if n != "地铁跑酷"][:2]
        if paid_pure:
            # 按排名升序（数字小=排名靠前）
            paid_pure.sort(key=lambda x: x[1])
            paid_str = "、".join(f"{n}（游戏畅销 #{pg}）" for n, pg in paid_pure)
            insights += f"""<div class="insight">六款游戏里只有两款是在游戏畅销榜上有排名的：{paid_str}（其余 4 款未进榜）。"高频曝光 + 持续付费用户"形成正反馈。</div>"""

    # ===== 单周冲榜洞察：尝试找出当周版本，附上玩法说明 =====
    if biggest_jump and biggest_jump["jump"] >= 5:
        # 找该周内发布的版本（如果有）
        jump_app = data["apps"].get(biggest_jump["name"], {})
        jump_versions = jump_app.get("versions", [])
        # 用 ISO 周来匹配
        from datetime import datetime as _dt
        related_v = None
        for v in jump_versions:
            vd = parse_cn_date(v.get("d"))
            if not vd:
                continue
            # biggest_jump['week'] 形如 "3/30–4/5"；用月日比较即可
            wk_str = biggest_jump["week"]
            try:
                start_md, end_md = wk_str.split("–")
                sm, sd = map(int, start_md.split("/"))
                em, ed = map(int, end_md.split("/"))
                year = vd.year
                w_start = date(year, sm, sd)
                w_end = date(year, em, ed)
                if w_start <= vd <= w_end:
                    related_v = v
                    break
            except Exception:
                pass

        ver_html = ""
        if related_v:
            # 从 release_note 抠出更新摘要前几个亮点
            note = (related_v.get("n") or "").replace("<br />", "\n").replace("<br/>", "\n")
            ver_html = f"""<br/>📌 同期发布版本 <b>v{related_v['v']}</b>（{related_v['d']}）：<i>{related_v.get('s','')}</i><br/>"""
            # 抽取关键玩法点
            highlights = []
            for line in note.split("\n"):
                line = line.strip()
                if not line:
                    continue
                # 取第一行非空（一般是该版本的核心介绍）+ 含「新增」「全新」「上线」「大更新」的行
                if any(k in line for k in ["双人", "组队对抗", "全新", "新增", "大更新", "首款"]):
                    highlights.append(line[:80])
                if len(highlights) >= 2:
                    break
            if highlights:
                ver_html += "<br/>".join(f"&nbsp;&nbsp;• {h}" for h in highlights)

        insights += f"""<div class="insight"><b>📈 {biggest_jump['name']} 在 {biggest_jump['week']} 周单周冲榜</b> — #{biggest_jump['from']} → #{biggest_jump['to']}（↑{biggest_jump['jump']} 名）。是 12 周内单周最大涨幅。{ver_html}</div>"""

    # 大版本对推荐位影响：保留原结论文案
    insights += """<div class="insight"><b>📊 大版本不必然冲榜</b> — 多个大版本发布周排名基本持平或微跌；版更对头部存量游戏的价值在于：<b>给 Apple Editor 一个"重新推荐"的理由</b>，而不是直接拉排名。</div>"""

    # ===== HTML =====
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=6)

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8" />
<title>{week_id} · 6 款休闲游戏排名周报</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 28px 20px 60px;
    font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei", sans-serif;
    background: #f4f5f9; color: #1d1d2c;
    -webkit-font-smoothing: antialiased;
  }}
  header {{ max-width: 1700px; margin: 0 auto 22px; }}
  h1 {{ margin: 0 0 6px; font-size: 24px; font-weight: 800; }}
  .sub {{ color: #6e7491; font-size: 13px; }}
  section {{ max-width: 1700px; margin: 0 auto 28px; }}
  h2 {{ font-size: 15px; color: #4a5276; margin: 24px 0 10px; font-weight: 700; }}
  h2 .badge {{ font-size: 11px; color: #6e7491; font-weight: 500; margin-left: 6px; }}
  .table-wrap {{
    background: #fff; border-radius: 12px; overflow: auto;
    box-shadow: 0 1px 3px rgba(0,0,0,0.04); border: 1px solid #ececf3;
  }}
  table {{ border-collapse: separate; border-spacing: 0; width: 100%; font-size: 12px; }}
  thead th {{
    background: #f9fafc; color: #6e7491;
    font-weight: 600; padding: 10px 8px;
    border-bottom: 1px solid #ececf3;
    white-space: nowrap; font-size: 11px;
    position: sticky; top: 0; z-index: 2;
  }}
  tbody td, tbody th {{
    padding: 10px 8px;
    border-bottom: 1px solid #f3f4fa;
    vertical-align: middle; text-align: left;
  }}
  tbody tr:last-child td, tbody tr:last-child th {{ border-bottom: none; }}
  tbody tr:hover {{ background: #fafbfd; }}
  .sticky-col {{
    position: sticky; left: 0; background: #fff !important; z-index: 1;
    border-right: 1px solid #ececf3;
    padding-left: 16px !important; min-width: 170px;
  }}
  thead th.sticky-col {{ background: #f9fafc !important; z-index: 3; }}
  .game-cell {{ display: flex; align-items: center; gap: 9px; }}
  .game-cell img {{ width: 28px; height: 28px; border-radius: 6px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
  .game-cell span {{ font-weight: 600; font-size: 12.5px; }}
  td .rank {{ font-weight: 700; color: #1d1d2c; font-size: 13px; }}
  td .delta {{ font-size: 10.5px; margin-top: 2px; font-weight: 600; }}
  .delta.up, .up {{ color: #ef4444; font-weight: 700; }}
  .delta.down, .down {{ color: #16a34a; font-weight: 700; }}
  .delta.neutral, .neutral {{ color: #9ca3af; font-weight: 500; }}
  td.empty {{ color: #d1d5db; text-align: center; }}
  td.num {{ font-variant-numeric: tabular-nums; font-weight: 600; text-align: center; }}
  td.nowrap {{ white-space: nowrap; }}
  .mono {{ font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 11.5px; }}
  .muted {{ color: #6e7491; font-size: 11px; line-height: 1.5; }}
  table.weekly tbody td {{ text-align: center; min-width: 88px; }}
  .version-group {{
    background: #fff; border-radius: 12px; padding: 14px 18px 10px;
    margin-bottom: 14px; box-shadow: 0 1px 3px rgba(0,0,0,0.04); border: 1px solid #ececf3;
  }}
  .version-group-head {{ display: flex; align-items: center; gap: 10px; margin-bottom: 12px; }}
  .version-group-head img {{ width: 30px; height: 30px; border-radius: 7px; }}
  .version-group-head h3 {{ margin: 0; font-size: 15px; font-weight: 700; }}
  .badge-count {{ background: #eef2ff; color: #4338ca; font-size: 11px; padding: 2px 7px; border-radius: 10px; font-weight: 600; }}
  .badge-empty {{ color: #9ca3af; font-size: 11px; }}
  .version-table thead th {{ position: static; }}
  .move {{ margin-left: 4px; }}
  .insight {{
    background: linear-gradient(135deg, #fef3c7, #fde68a);
    color: #78350f; border-radius: 10px;
    padding: 14px 18px; font-size: 13px; line-height: 1.7;
    margin-bottom: 10px;
  }}
  .insight b {{ color: #92400e; }}
  /* Featured details (collapsible) */
  .detail-block {{
    background: #fff; border-radius: 12px; margin-bottom: 8px;
    box-shadow: 0 1px 3px rgba(0,0,0,0.04); border: 1px solid #ececf3;
    overflow: hidden;
  }}
  .detail-block summary {{
    cursor: pointer; padding: 12px 16px;
    display: flex; align-items: center; gap: 10px;
    user-select: none; list-style: none;
  }}
  .detail-block summary::-webkit-details-marker {{ display: none; }}
  .detail-block summary img {{ width: 26px; height: 26px; border-radius: 6px; }}
  .detail-block summary .dname {{ font-weight: 700; font-size: 13px; flex: 1; }}
  .detail-block summary .dcount {{ background: #f0fdf4; color: #16a34a; font-size: 11px; padding: 2px 8px; border-radius: 10px; font-weight: 600; }}
  .detail-block summary .darrow {{ color: #9ca3af; font-size: 10px; transition: transform .2s; }}
  .detail-block[open] summary .darrow {{ transform: rotate(180deg); }}
  .detail-block summary:hover {{ background: #fafbfd; }}
  .detail-block .table-wrap {{ border: none; border-radius: 0; box-shadow: none; border-top: 1px solid #ececf3; }}
  .nav-back {{
    display: inline-block; margin-bottom: 14px;
    color: #6e7491; text-decoration: none; font-size: 12px;
    padding: 4px 10px; border: 1px solid #ececf3; border-radius: 16px;
    background: #fff;
  }}
  .nav-back:hover {{ color: #2563eb; }}
</style>
</head>
<body>

<header>
  <a href="index.html" class="nav-back">← 返回历史周报列表</a>
  <h1>📊 {week_id} · 6 款休闲游戏排名周报</h1>
  <div class="sub">中国区 · App Store iPhone · 游戏分类免费榜 · 周维度（周一~周日均值）· 数据窗口 {data['window']['sdate']} ~ {data['window']['edate']} · 生成于 {datetime.now().strftime('%Y-%m-%d %H:%M')} · 数据来源 七麦数据</div>
</header>

<section>
  <h2>① 整体趋势速览 <span class="badge">实时 + 本周 + 12 周首尾对比</span></h2>
  <div class="table-wrap">
    <table>
      <thead>
        <tr>
          <th class="sticky-col" style="text-align:left; padding-left:16px;">游戏</th>
          <th>实时·游戏免费</th>
          <th>本周（游戏免费）</th>
          <th>上周（游戏免费）</th>
          <th>本周 vs 上周</th>
          <th>实时·游戏畅销</th>
          <th>12 周前</th>
          <th>最高峰</th>
          <th>最低谷</th>
          <th>首尾变动</th>
          <th>窗口内版本数</th>
          <th>窗口内推荐次数</th>
        </tr>
      </thead>
      <tbody>{summary_rows}</tbody>
    </table>
  </div>
</section>

<section>
  <h2>② 周维度 · 游戏分类免费榜排名 <span class="badge">每格 = 当周排名 + 较上周变动</span></h2>
  <div class="insight">
    <b>📍 怎么读：</b> 每格上方是当周"游戏分类免费榜"平均排名，下方是与<b>上一周</b>对比的名次变动。<b>↑ 红色 = 名次提升</b>（数字变小），<b>↓ 绿色 = 名次下降</b>。中国惯例红涨绿跌。
  </div>
  <div class="table-wrap">
    <table class="weekly">
      <thead>
        <tr>
          <th class="sticky-col" style="text-align:left; padding-left:16px;">游戏 ＼ 周</th>
          {''.join(f'<th>{w}</th>' for w in reversed(week_labels))}
        </tr>
      </thead>
      <tbody>{rank_rows}</tbody>
    </table>
  </div>
</section>

<section>
  <h2>③ 窗口期版本更新明细 <span class="badge">分游戏列出，含版更前 7 天 vs 后 7 天的排名变动</span></h2>
  {version_blocks}
</section>

<section>
  <h2>④ Apple 精品推荐位情况</h2>
  <h3 style="font-size:13px; color:#6e7491; margin:8px 0 12px; font-weight:600;">明细：每款游戏的新增推荐位清单（点击展开）</h3>
  {featured_details}
</section>

<section>
  <h2>⑤ 关键洞察</h2>
  {insights}
</section>

<footer style="max-width:1700px; margin: 30px auto 0; color:#6e7491; font-size:11px; text-align:center;">
  数据抓取于 {datetime.now().strftime('%Y-%m-%d %H:%M')} · 来源 <a href="https://www.qimai.cn" target="_blank" style="color:#2563eb;">七麦数据</a> · 周报存档 ID: {week_id}
</footer>

</body>
</html>
"""
    return html

# ============================================================
# 列表页 index.html
# ============================================================
def render_index(reports_dir: Path):
    files = sorted(reports_dir.glob("20*-w*.html"), reverse=True)
    rows = ""
    for f in files:
        wk = f.stem  # e.g. 2026-w20
        m = re.match(r"(\d{4})-w(\d+)", wk)
        if not m: continue
        year = int(m.group(1)); week = int(m.group(2))
        # ISO week → date range
        d0 = date.fromisocalendar(year, week, 1)
        d1 = d0 + timedelta(days=6)
        mtime = datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        rows += f"""<tr>
          <td><a href="{f.name}" class="link">{wk}</a></td>
          <td class="muted">{d0.strftime('%Y-%m-%d')} ~ {d1.strftime('%Y-%m-%d')}</td>
          <td class="muted">{mtime}</td>
        </tr>"""

    if not rows:
        rows = '<tr><td colspan="3" class="muted" style="text-align:center; padding:30px;">还没有任何周报，第一次跑脚本就会生成</td></tr>'

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8" />
<title>6 款休闲游戏 · 周报存档</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 32px 20px;
    font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei", sans-serif;
    background: #f4f5f9; color: #1d1d2c;
  }}
  .wrap {{ max-width: 720px; margin: 0 auto; }}
  h1 {{ margin: 0 0 6px; font-size: 26px; }}
  .sub {{ color: #6e7491; font-size: 13px; margin-bottom: 24px; }}
  .table-wrap {{
    background: #fff; border-radius: 12px; overflow: hidden;
    box-shadow: 0 1px 3px rgba(0,0,0,0.04); border: 1px solid #ececf3;
  }}
  table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
  thead th {{ background: #f9fafc; color: #6e7491; font-weight: 600; padding: 12px 16px; text-align: left; font-size: 12px; border-bottom: 1px solid #ececf3; }}
  tbody td {{ padding: 14px 16px; border-bottom: 1px solid #f3f4fa; }}
  tbody tr:last-child td {{ border-bottom: none; }}
  tbody tr:hover {{ background: #fafbfd; }}
  .link {{ color: #2563eb; text-decoration: none; font-weight: 600; }}
  .link:hover {{ text-decoration: underline; }}
  .muted {{ color: #6e7491; font-size: 12px; }}
  .games {{ margin-top: 32px; padding: 16px 20px; background: #fff; border-radius: 12px; border: 1px solid #ececf3; }}
  .games h3 {{ margin: 0 0 10px; font-size: 13px; color: #6e7491; font-weight: 600; }}
  .games .row {{ display: flex; flex-wrap: wrap; gap: 6px; }}
  .games .tag {{ font-size: 12px; padding: 4px 10px; background: #f4f5f9; border-radius: 12px; }}
</style>
</head>
<body>
<div class="wrap">
  <h1>📊 6 款休闲游戏 · 周报存档</h1>
  <div class="sub">中国区 · App Store iPhone · 游戏分类免费榜 · 数据来源 七麦数据</div>

  <div class="table-wrap">
    <table>
      <thead>
        <tr><th>周报</th><th>覆盖周</th><th>生成时间</th></tr>
      </thead>
      <tbody>{rows}</tbody>
    </table>
  </div>

  <div class="games">
    <h3>跟踪的 6 款产品</h3>
    <div class="row">
      {''.join(f'<span class="tag">{g["name"]}</span>' for g in GAMES)}
    </div>
  </div>
</div>
</body>
</html>
"""
    (reports_dir / "index.html").write_text(html, encoding="utf-8")
    print(f"  ✓ 列表页已更新：{reports_dir / 'index.html'}")

# ============================================================
# main
# ============================================================
def main():
    print("📊 七麦数据周报生成器")
    print("=" * 50)

    # ISO week
    today = date.today()
    iso_year, iso_week, _ = today.isocalendar()
    week_id = f"{iso_year}-w{iso_week:02d}"
    print(f"本周编号：{week_id}")

    tab = find_qimai_tab()
    print(f"✓ 已找到登录态 Chrome 标签：{tab['url'][:60]}...")

    print("\n🌐 抓取数据中...")
    raw = fetch_all_data(tab)

    print("\n📦 处理数据...")
    processed = build_processed(raw)
    json_path = REPORTS_DIR / f"{week_id}.json"
    json_path.write_text(json.dumps(processed, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  ✓ JSON 已保存：{json_path}")

    print("\n🎨 生成 HTML...")
    html = render_html(processed, week_id)
    html_path = REPORTS_DIR / f"{week_id}.html"
    html_path.write_text(html, encoding="utf-8")
    print(f"  ✓ 周报已保存：{html_path}")

    print("\n📚 更新列表页...")
    render_index(REPORTS_DIR)

    # ============================================================
    # 自动 git push（仅当当前目录是 git 仓库且远程已配置）
    # ============================================================
    import subprocess
    git_dir = REPORTS_DIR / ".git"
    if git_dir.exists():
        print("\n🚀 推送到 GitHub Pages...")
        try:
            # 检查是否配置了 remote
            r = subprocess.run(["git", "remote"], cwd=REPORTS_DIR, capture_output=True, text=True)
            if "origin" not in r.stdout:
                print("  ⚠ 未配置 git remote origin，跳过推送")
                print("    （首次配置见 README，或问 Claude）")
            else:
                subprocess.run(["git", "add", "-A"], cwd=REPORTS_DIR, check=True)
                # 检查是否有变化要提交
                status = subprocess.run(["git", "status", "--porcelain"], cwd=REPORTS_DIR, capture_output=True, text=True)
                if not status.stdout.strip():
                    print("  ℹ 无新内容，跳过提交")
                else:
                    msg = f"周报 {week_id} · {datetime.now().strftime('%Y-%m-%d %H:%M')}"
                    subprocess.run(["git", "commit", "-m", msg], cwd=REPORTS_DIR, check=True, capture_output=True)
                    push = subprocess.run(["git", "push"], cwd=REPORTS_DIR, capture_output=True, text=True)
                    if push.returncode == 0:
                        print(f"  ✓ 已推送到 GitHub")
                        print(f"  🌐 公网链接 30 秒后更新：https://selena-chen1.github.io/qimai-reports/")
                    else:
                        print(f"  ⚠ git push 失败：{push.stderr.strip()[:300]}")
        except subprocess.CalledProcessError as e:
            print(f"  ⚠ git 命令失败：{e}")
        except Exception as e:
            print(f"  ⚠ 推送异常：{e}")

    print("\n" + "=" * 50)
    print("✅ 完成！")
    print(f"   本周报告：file://{html_path}")
    print(f"   存档列表：file://{REPORTS_DIR / 'index.html'}")
    print(f"\n💡 飞书分享：可直接把 {html_path.name} 拖入飞书云文档，或截图发群")

if __name__ == "__main__":
    main()
