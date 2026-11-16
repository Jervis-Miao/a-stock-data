"""Market and portfolio views for the Streamlit dashboard."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime
from math import isfinite
from pathlib import Path
import re
import sqlite3
from time import monotonic, time
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import streamlit as st

PORTFOLIO_DB = Path(__file__).with_name(".portfolio.sqlite3")
SHANGHAI = ZoneInfo("Asia/Shanghai")
STOCK_CODE = re.compile(r"(?:60|68|00|30|43|83|87|88|92)[0-9]{4}")
SOURCE_ERRORS = (OSError, RuntimeError, ValueError, requests.RequestException)


def stock_code_button(key, label="股票代码"):
    return st.column_config.ButtonColumn(
        label, type="tertiary", help="点击复制股票代码", key=key,
    )


def copy_clicked_stock_code(key):
    click = st.session_state.get(key)
    code = click["label"] if click else ""
    if not re.fullmatch(r"[0-9]{6}", code):
        return
    st.html(
        f"""<small role="status" id="{key}_copy_status"></small>
        <script>
        (async () => {{
            const status = document.getElementById("{key}_copy_status");
            try {{
                await navigator.clipboard.writeText("{code}");
                if (status) status.textContent = "已复制 {code}";
            }} catch {{
                if (status) status.textContent = "剪贴板不可用，请手动复制 {code}";
            }}
        }})();
        </script>""",
        unsafe_allow_javascript=True,
    )


def market_move_color(value):
    if pd.isna(value) or value == 0:
        return ""
    return "color: #c62828" if value > 0 else "color: #087843"


def market_sources():
    return load_market_sources(Path(__file__).with_name("SKILL.md").read_text(encoding="utf-8"))


@st.cache_resource
def load_market_sources(skill):
    blocks = re.findall(r"```python\n(.*?)\n```", skill, re.S)
    namespace = {}
    for function, tail in (
        ("em_get", None),
        ("tencent_quote", "# 用法: 个股"),
        ("board_fund_flow", "# 用法"),
        ("em_price_anomaly", "# 用法"),
        ("eastmoney_concept_blocks", "# 用法"),
    ):
        block = next(code for code in blocks if re.search(rf"^def {function}\(", code, re.M))
        exec(compile(block.split(tail, 1)[0] if tail else block, "SKILL.md", "exec"), namespace)
    board_fund_flow = namespace["board_fund_flow"]
    namespace["board_gainers"] = lambda category: board_fund_flow(category, "today", 10, sort_by_change=True)
    namespace["em_market_code"] = lambda code: int(equity_symbol(code).startswith("sh"))
    return namespace


@st.cache_resource
def market_workers(provider):
    return ThreadPoolExecutor(max_workers=1, thread_name_prefix=provider)


def fetch(source, args):
    payload = source(*args)
    return datetime.now(SHANGHAI), payload


INTERNATIONAL_INDICES = (
    ("美国", "标普 500", "^GSPC"),
    ("美国", "纳斯达克综合指数", "^IXIC"),
    ("美国", "道琼斯工业平均指数", "^DJI"),
    ("欧洲", "德国 DAX", "^GDAXI"),
    ("欧洲", "英国富时 100", "^FTSE"),
    ("亚洲", "日经 225", "^N225"),
    ("亚洲", "韩国综合指数（KOSPI）", "^KS11"),
    ("亚洲", "恒生指数", "^HSI"),
)


def international_indices():
    rows = []
    quote_errors = (*SOURCE_ERRORS, KeyError, IndexError, TypeError)
    with requests.Session() as session:
        session.headers["User-Agent"] = "Mozilla/5.0"
        for region, name, symbol in INTERNATIONAL_INDICES:
            row = {"市场": region, "指数": name}
            try:
                response = session.get(
                    f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
                    params={"range": "1d", "interval": "1d"},
                    timeout=(3, 5),
                )
                response.raise_for_status()
                meta = response.json()["chart"]["result"][0]["meta"]
                price = float(meta["regularMarketPrice"])
                previous = float(meta["chartPreviousClose"])
                if not (isfinite(price) and isfinite(previous) and price > 0 and previous > 0):
                    raise ValueError("无有效报价")
                row.update({
                    "最新点位": price,
                    "涨跌幅": (price / previous - 1) * 100,
                    "报价时间（北京时间）": datetime.fromtimestamp(meta["regularMarketTime"], SHANGHAI).strftime("%m-%d %H:%M"),
                })
            except quote_errors:
                row.update({"最新点位": None, "涨跌幅": None, "报价时间（北京时间）": "暂无报价"})
            rows.append(row)
    if all(row["最新点位"] is None for row in rows):
        raise RuntimeError("国际指数暂无报价")
    return rows


@st.fragment(run_every=2)
def international_view():
    st.subheader("国际行情")
    summary, actions = st.columns([3, 1], vertical_alignment="center")
    summary.caption("主要海外指数 · Yahoo Finance · 页面打开时每分钟检查更新")
    refresh = actions.button("立即刷新国际行情", icon=":material/refresh:", width="stretch")
    rows = market_snapshot("international_indices", (), refresh)
    if rows:
        frame = pd.DataFrame(rows)
        st.dataframe(
            frame.style.map(market_move_color, subset=["涨跌幅"]), hide_index=True,
            column_config={
                "最新点位": st.column_config.NumberColumn(format="%.2f"),
                "涨跌幅": st.column_config.NumberColumn(format="%.2f%%"),
            },
        )
        st.caption("各市场交易时段不同。报价可能延迟，休市时显示最近一笔行情。")


SECTOR_DB = Path(__file__).with_name(".market-data") / "sectors.sqlite3"
SECTOR_TTL = 12 * 3600


def sector_cache(codes, update=None):
    SECTOR_DB.parent.mkdir(exist_ok=True)
    with closing(sqlite3.connect(SECTOR_DB)) as db, db:
        db.execute("CREATE TABLE IF NOT EXISTS stock_sectors (code TEXT PRIMARY KEY, names TEXT, fetched REAL)")
        if update is not None:
            db.execute("INSERT OR REPLACE INTO stock_sectors VALUES (?, ?, ?)", (*update, time()))
        if not codes:
            return {}
        placeholders = ",".join("?" for _ in codes)
        return {code: (names, fetched) for code, names, fetched in db.execute(
            f"SELECT code, names, fetched FROM stock_sectors WHERE code IN ({placeholders})", codes,
        )}


def stock_sectors(lookup, codes):
    cached = sector_cache(codes)
    missing = []
    for code in codes:
        if code in cached and time() - cached[code][1] < SECTOR_TTL:
            continue
        tags = lookup(code)["concept_tags"]
        names = list(dict.fromkeys(name for name in tags if name))
        if names:
            sector_cache((), (code, "、".join(names[:3])))
        else:
            missing.append(code)
    if missing:
        raise RuntimeError("部分股票暂无板块归属")


@st.fragment(run_every=2)
def selection_table(frame, fetch_sectors):
    if fetch_sectors:
        codes = tuple(code for code, sector in frame["所属板块"].items()
                      if sector == "—" and STOCK_CODE.fullmatch(code))
        cached = sector_cache(codes) if codes else {}
        now = time()
        if any(code not in cached or now - cached[code][1] >= SECTOR_TTL for code in codes):
            market_snapshot("stock_sectors", (codes,), refresh_seconds=SECTOR_TTL)
        frame = frame.copy()
        for code, (names, _) in cached.items():
            frame.at[code, "所属板块"] = names
        st.caption("板块来自东方财富当前归属，最多显示三个。已查到的板块会保存到本地。")
    st.dataframe(
        frame.round(2).reset_index(), hide_index=True,
        column_config={"股票代码": stock_code_button("copy_selection_code")},
    )
    copy_clicked_stock_code("copy_selection_code")


def market_snapshot(function, args, manual=False, *, refresh_seconds=60):
    key = (function, args)
    states = st.session_state.setdefault("market_requests", {})
    state = states.setdefault(key, {})
    future = state.get("future")
    if future is not None and future.done():
        state.pop("future")
        try:
            state["snapshot"] = future.result()
            state.pop("error", None)
            state["next"] = monotonic() + refresh_seconds
        except SOURCE_ERRORS as exc:
            state["error"] = type(exc).__name__
            state["next"] = monotonic() + 180
        future = None

    previous = state.get("snapshot")
    remaining = state.get("next", 0) - monotonic()
    if future is None and (remaining <= 0 or (manual and "error" not in state)):
        if function == "international_indices":
            source, provider = international_indices, "yahoo"
        else:
            sources = market_sources()
            if function == "stock_sectors":
                source = stock_sectors
                args = (sources["eastmoney_concept_blocks"], *args)
            else:
                source = sources[function]
            provider = "tencent" if function == "tencent_quote" else "eastmoney"
        state["future"] = market_workers(provider).submit(fetch, source, args)
        st.caption("正在后台加载行情…" if previous is None else "正在后台更新行情…")
    elif future is not None:
        st.caption("正在后台加载行情…" if previous is None else "正在后台更新行情…")
    elif "error" in state:
        status = "继续显示上次快照" if previous else "暂无可用快照"
        st.warning(f"行情暂时无法连接（{state['error']}），{status}。约 {int(remaining) + 1} 秒后重试。")

    if previous is not None:
        timestamp, payload = previous
        st.caption(f"快照查询时间（北京时间） {timestamp:%Y-%m-%d %H:%M:%S}")
        return payload
    return None


@st.fragment(run_every=2)
def market_view():
    summary, actions = st.columns([3, 1], vertical_alignment="center")
    summary.caption("每分钟刷新 · 北京时间 · 休市时可能显示上一交易日行情")
    manual = actions.button("立即刷新行情", icon=":material/refresh:", width="stretch")
    with st.container(border=True):
        st.subheader("板块涨幅")
        category = st.selectbox("板块分类", ["行业", "概念"])
        industry = "industry" if category == "行业" else "concept"
        result = market_snapshot("board_gainers", (industry,), manual)
        rows = pd.DataFrame(result["rows"]) if result else pd.DataFrame()
        if result is not None and rows.empty:
            st.info("当前没有可展示的板块行情。")
        elif not rows.empty:
            rows["change_pct"] = pd.to_numeric(rows["change_pct"], errors="coerce")
            rows["main_net"] = pd.to_numeric(rows["main_net"], errors="coerce") / 1e8
            display = rows.dropna(subset=["change_pct"]).nlargest(10, "change_pct")
            st.caption(f"东方财富板块快照 · 显示 {len(display)} 个板块 · 按涨幅排序")
            st.dataframe(display.rename(columns={
                "name": "板块", "code": "板块代码", "change_pct": "涨跌幅（%）",
                "main_net": "主力净额（亿元）", "leader": "领涨股",
            })[["板块", "板块代码", "涨跌幅（%）", "主力净额（亿元）", "领涨股"]]
                         .style.map(market_move_color, subset=["涨跌幅（%）"]), hide_index=True)
            if not display.empty:
                selected = st.selectbox("查看板块资金明细", display.index, format_func=lambda index: display.at[index, "name"])
                row = display.loc[selected]
                flows = pd.Series(
                    [row["super_large_net"], row["large_net"], row["medium_net"], row["small_net"]],
                    index=["超大单", "大单", "中单", "小单"],
                )
                st.bar_chart(pd.to_numeric(flows, errors="coerce").div(1e8).rename("净额（亿元）"))
    with st.container(border=True):
        st.subheader("上涨异动")
        result = market_snapshot("em_price_anomaly", (200, 1), manual)
        rows = pd.DataFrame(result["items"]) if result else pd.DataFrame()
        if result is not None and rows.empty:
            st.info("当前没有可展示的异动记录。")
        elif not rows.empty:
            rows["change_pct"] = pd.to_numeric(rows["change_pct"], errors="coerce")
            display = rows.loc[rows["is_today"] & rows["change_pct"].gt(0)]
            st.caption(f"行情日期 {result['date']} · 交易所严重异常波动口径 · 第一页")
            if display.empty:
                st.info("该页没有触发上涨异动的记录。")
            else:
                st.dataframe(display.sort_values("change_pct", ascending=False).rename(columns={
                    "code": "代码", "name": "名称", "change_pct": "涨跌幅（%）", "rule": "异动原因",
                })[["代码", "名称", "涨跌幅（%）", "异动原因"]]
                             .style.map(market_move_color, subset=["涨跌幅（%）"]), hide_index=True,
                             column_config={"代码": stock_code_button("copy_anomaly_code", "代码")})
                copy_clicked_stock_code("copy_anomaly_code")


def positions(action=None, values=()):
    with closing(sqlite3.connect(PORTFOLIO_DB)) as db, db:
        db.execute("CREATE TABLE IF NOT EXISTS positions (code TEXT PRIMARY KEY, shares INTEGER, cost REAL)")
        if action == "保存":
            db.execute("INSERT OR REPLACE INTO positions VALUES (?, ?, ?)", values)
        elif action == "批量更新":
            db.executemany(
                "UPDATE positions SET shares = COALESCE(?, shares), cost = COALESCE(?, cost) WHERE code = ?",
                values,
            )
        elif action == "删除":
            db.execute("DELETE FROM positions WHERE code = ?", values)
        return db.execute("SELECT code, shares, cost FROM positions ORDER BY code").fetchall()


def equity_symbol(code):
    if code.startswith(("60", "68")):
        return "sh" + code
    if code.startswith(("00", "30")):
        return "sz" + code
    return "bj" + code


def portfolio_view():
    st.subheader("我的持仓")
    if feedback := st.session_state.pop("position_feedback", None):
        st.success(feedback)

    holdings = positions()
    if holdings:
        portfolio_quotes()
    else:
        manage_positions(holdings)
        st.info("暂无持仓。填写上方表单后，持仓与报价会显示在这里。")


def manage_positions(holdings, names=None):
    with st.expander("管理持仓", expanded=not holdings):
        with st.form("position"):
            symbol, volume, entry = st.columns(3)
            code = symbol.text_input("股票代码（六位）").strip()
            shares = volume.number_input("持股数量（股）", 1, 1000000000, 100, step=100)
            cost = entry.number_input("每股成本（元）", 0.0001, 1000000.0, 10.0, format="%.2f")
            st.caption("输入已有股票代码可更新该持仓。")
            save = st.form_submit_button("保存持仓", type="primary")
        if save:
            if STOCK_CODE.fullmatch(code):
                positions("保存", (code, shares, cost))
                st.session_state["position_feedback"] = f"已保存 {code}。"
                st.rerun()
            st.error("请输入六位 A 股代码。")

        if holdings:
            names = names or {}

            def holding_label(code):
                if code is None:
                    return "请选择"
                name = names.get(code)
                return f"{name}（{code}）" if name else code

            remove = st.selectbox(
                "删除持仓", [None, *[row[0] for row in holdings]],
                format_func=holding_label, key="position_to_remove",
            )
            if st.button("删除所选持仓", disabled=remove is None):
                positions("删除", (remove,))
                st.session_state["position_feedback"] = f"已删除 {remove}。"
                st.rerun()


@st.cache_resource
def column_reorder_widget(script):
    return st.components.v2.component(
        "portfolio_column_reorder",
        js=script,
    )


def portfolio_column_order(columns, editor_key):
    key = "portfolio_column_order"
    order = [column for column in st.session_state.get(key, []) if column in columns]
    order.extend(column for column in columns if column not in order)
    script = Path(__file__).with_name("portfolio-columns.js").read_text(encoding="utf-8")
    result = column_reorder_widget(script)(
        key="portfolio_column_reorder",
        data={"columns": order, "editorKey": editor_key},
        on_order_change=lambda: None,
    )
    if isinstance(result.order, list) and len(result.order) == len(columns) and set(result.order) == set(columns):
        order = result.order
        st.session_state[key] = order
    return order


def save_position_cells(key, codes):
    edits = st.session_state[key]["edited_rows"]
    changes = []
    for row, fields in edits.items():
        quantity = fields.get("数量（股）")
        cost = fields.get("成本价（元）")
        bad_quantity = "数量（股）" in fields and (
            pd.isna(quantity) or not 1 <= quantity <= 1000000000 or int(quantity) != quantity
        )
        bad_cost = "成本价（元）" in fields and (pd.isna(cost) or not 0.0001 <= cost <= 1000000)
        if bad_quantity or bad_cost:
            st.session_state["position_edit_error"] = "数量须为正整数，成本须为 0.0001 至 1,000,000 元。"
            break
        changes.append((int(quantity) if quantity is not None else None, cost, codes[row]))
    else:
        positions("批量更新", changes)
        st.session_state["position_edit_saved"] = bool(changes)
    st.session_state["position_editor_revision"] += 1


def holding_move(quote):
    change = quote.get("change_pct")
    if quote.get("is_stale") or quote.get("price", 0) <= 0 or quote.get("last_close", 0) <= 0:
        return None, "无法判断"
    if change is None or not isfinite(change):
        return None, "无法判断"
    if change > 2:
        return change, "拉升异动"
    if change < -2:
        return change, "下跌异动"
    return change, "—"


@st.fragment(run_every=2)
def portfolio_quotes():
    portfolio_quotes_content()


def portfolio_quotes_content():
    holdings = positions()
    codes = tuple(equity_symbol(code) for code, _, _ in holdings)
    if not codes:
        return
    summary, actions = st.columns([3, 1], vertical_alignment="center")
    summary.caption("腾讯财经报价 · 页面打开时每分钟更新")
    manual = actions.button("立即刷新持仓报价", icon=":material/refresh:", width="stretch")
    quotes = market_snapshot("tencent_quote", (codes,), manual) or {}

    declines = []
    alerts = []
    rows = []
    for code, quantity, unit_cost in holdings:
        quote = quotes.get(equity_symbol(code), {})
        move, alert = holding_move(quote)
        if alert == "下跌异动":
            declines.append(f"- {quote.get('name') or code}（{code}） {move:+.2f}%")
        elif alert == "拉升异动":
            alerts.append(f"{alert} · {quote.get('name') or code}（{code}） {move:+.2f}%")
        price = quote.get("price", 0)
        value = price * quantity if price > 0 else None
        rows.append({
            "代码": code, "名称": quote.get("name"), "数量（股）": quantity, "成本价（元）": unit_cost,
            "最新价（元）": price if value is not None else None,
            "相对昨收涨跌幅（%）": move, "异动": alert,
            "市值（元）": value,
            "浮动盈亏（元）": (price - unit_cost) * quantity if value is not None else None,
            "浮动盈亏率（%）": (price / unit_cost - 1) * 100 if value is not None and unit_cost > 0 else None,
            "报价状态": "无报价" if value is None else ("无成交或旧报价" if quote.get("is_stale") else "有报价"),
        })
    def show_alerts():
        if declines:
            st.success(f"**下跌异动（{len(declines)} 只）**\n\n" + "\n".join(declines))
        for message in alerts:
            st.error(message)

    frame = pd.DataFrame(rows)
    if frame["市值（元）"].notna().all():
        total_value = frame["市值（元）"].sum()
        total_profit = frame["浮动盈亏（元）"].sum()
        value_card, profit_card, count_card = st.columns(3)
        value_card.metric("持仓市值", f"¥{total_value:,.2f}")
        profit_card.metric("浮动盈亏", f"¥{total_profit:,.2f}")
        count_card.metric("持仓股票", len(frame))
        frame["持仓市值占比（%）"] = frame["市值（元）"] / frame["市值（元）"].sum() * 100
    show_alerts()
    revision = st.session_state.setdefault("position_editor_revision", 0)
    editor_key = f"position_values_editor_{revision}"
    editable = frame.round(2)
    editable["成本价（元）"] = frame["成本价（元）"]
    order = portfolio_column_order(list(editable.columns), editor_key)
    styled = editable.style.map(
        market_move_color, subset=["浮动盈亏（元）", "浮动盈亏率（%）", "相对昨收涨跌幅（%）"],
    )
    styled.apply(
        lambda prices: frame.loc[prices.index, "相对昨收涨跌幅（%）"].map(market_move_color),
        subset=["最新价（元）"],
    )
    decimal_columns = (
        "最新价（元）", "相对昨收涨跌幅（%）", "市值（元）",
        "浮动盈亏（元）", "浮动盈亏率（%）", "持仓市值占比（%）",
    )
    names = {code: quotes.get(equity_symbol(code), {}).get("name") for code, _, _ in holdings}
    manage_positions(holdings, names)
    st.data_editor(
        styled, hide_index=True, key=editor_key, column_order=order,
        disabled=[column for column in editable if column not in ("数量（股）", "成本价（元）")],
        column_config={
            "代码": stock_code_button("copy_holding_code", "代码"),
            **{column: st.column_config.NumberColumn(format="%.2f") for column in decimal_columns},
            "相对昨收涨跌幅（%）": st.column_config.NumberColumn("涨跌幅", format="%.2f%%"),
            "持仓市值占比（%）": st.column_config.NumberColumn("市值占比", format="%.2f%%"),
            "浮动盈亏率（%）": st.column_config.NumberColumn("盈亏率", format="%.2f%%"),
            "浮动盈亏（元）": st.column_config.NumberColumn("盈亏", format="%.2f"),
            "数量（股）": st.column_config.NumberColumn(min_value=1, max_value=1000000000, step=100, required=True),
            "成本价（元）": st.column_config.NumberColumn(min_value=0.0001, max_value=1000000, format="%.2f", required=True),
        },
        on_change=save_position_cells,
        args=(editor_key, tuple(code for code, _, _ in holdings)),
    )
    copy_clicked_stock_code("copy_holding_code")
    if error := st.session_state.pop("position_edit_error", None):
        st.error(error)
    elif st.session_state.pop("position_edit_saved", False):
        st.toast("持仓已自动保存")
    st.caption("来源：腾讯财经。查询时间是请求时间，休市时可能返回上一个交易日的报价。")


def swing_view(history, held):
    st.subheader("一周波段观察")
    if history.empty:
        st.info("请先在「选股与回测」更新真实行情。波段信号至少需要 21 个交易日。")
        return
    close = history.pivot(index="date", columns="code", values="close").sort_index()
    amount = history.pivot(index="date", columns="code", values="amount").reindex_like(close)
    if len(close) < 21:
        st.info(f"目前只有 {len(close)} 个交易日，需要至少 21 个交易日。")
        return
    latest = close.iloc[-1]
    five_day = latest / close.iloc[-6] - 1
    trend = close.tail(20).mean().where(close.tail(20).count().eq(20))
    turnover = amount.tail(5).mean().where(amount.tail(5).count().eq(5))
    names = history.loc[history["date"].eq(close.index[-1])].set_index("code")["name"]
    st.caption(f"信号日期 {close.index[-1]:%Y-%m-%d} · 通达信盘后数据 · 观察周期约五个交易日")
    holdings_panel = st.container(border=True)
    holdings_panel.markdown("**持仓操作建议**")
    signals = []
    for code in held:
        price, average, weekly = latest.get(code), trend.get(code), five_day.get(code)
        if any(pd.isna(value) for value in (price, average, weekly)):
            note = "日线不足"
            action = "补齐盘后行情后再评估"
        elif price < average:
            note = "收盘低于 20 日均线"
            action = "暂停加仓。若下一交易日收盘仍低于 20 日均线，考虑减仓"
        elif weekly < 0:
            note = "近五日下跌，收盘仍在 20 日均线上方"
            action = "暂持观望、暂停加仓。若收盘跌破 20 日均线，重新评估仓位"
        else:
            note = "收盘位于 20 日均线或以上，近五日未下跌"
            action = "继续持有，每日复核。若近五日涨幅转负，转为观望"
        signals.append({"代码": code, "名称": names.get(code), "观察信号": note, "操作建议": action})
    if signals:
        holdings_panel.caption("操作建议由盘后趋势规则生成，按下一交易日收盘重新评估。")
        holdings_panel.dataframe(
            pd.DataFrame(signals), hide_index=True,
            column_config={"代码": stock_code_button("copy_swing_holding_code", "代码")},
        )
        with holdings_panel:
            copy_clicked_stock_code("copy_swing_holding_code")
    else:
        holdings_panel.info("录入持仓后显示对应的日线信号。")

    candidates_panel = st.container(border=True)
    candidates_panel.markdown("**进攻候选**")
    min_amount = candidates_panel.number_input(
        "近五日日均成交额下限（万元）", min_value=0, value=5000, step=1000,
    )
    eligible = five_day.gt(0) & latest.gt(trend) & turnover.ge(min_amount * 10000)
    eligible &= ~five_day.index.isin(held)
    top = five_day[eligible].nlargest(10)
    if top.empty:
        candidates_panel.info("当前没有符合条件的候选。")
        return
    candidates_panel.caption("近五日上涨、收盘在 20 日均线上方、近五日日均成交额达标。后续收盘跌破 20 日均线时，趋势条件失效。")
    candidates_panel.dataframe(pd.DataFrame({
        "代码": top.index, "名称": names.reindex(top.index).to_numpy(),
        "近五日涨幅（%）": (top * 100).round(2).to_numpy(),
        "日均成交额（万元）": (turnover.reindex(top.index) / 10000).round(2).to_numpy(),
    }), hide_index=True, column_config={"代码": stock_code_button("copy_candidate_code", "代码")})
    with candidates_panel:
        copy_clicked_stock_code("copy_candidate_code")
