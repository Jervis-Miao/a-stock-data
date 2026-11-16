"""Local A-share momentum research dashboard."""
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import random
import re
from tempfile import NamedTemporaryFile

import pandas as pd
import streamlit as st

from workspace_dashboard import copy_clicked_stock_code, international_view, market_view, market_workers, portfolio_view, positions, selection_table, stock_code_button, swing_view


@st.cache_data
def demo_data():
    rng = random.Random(42)
    rows = []
    dates = pd.bdate_range("2025-01-02", periods=160)
    for index in range(32):
        price = rng.uniform(10, 80)
        for day in dates:
            opening = price * rng.uniform(0.99, 1.01)
            price = opening * (1 + rng.gauss(0.0003, 0.018))
            rows.append((day, f"示例{index + 1:02}", opening, price, rng.uniform(2e7, 8e8)))
    return pd.DataFrame(rows, columns=["date", "code", "open", "close", "amount"])


def read_history(file):
    frame = pd.read_csv(file, dtype={"code": "string"})
    required = ["date", "code", "open", "close", "amount"]
    if not set(required).issubset(frame.columns):
        raise ValueError("CSV 必须包含 date、code、open、close、amount。")
    frame = frame[required + [column for column in ("name", "sector") if column in frame]].copy()
    frame["date"] = pd.to_datetime(frame["date"], format="%Y-%m-%d").dt.normalize()
    prices = ["open", "close", "amount"]
    frame[prices] = frame[prices].apply(pd.to_numeric)
    if (
        frame.empty or frame[required].isna().any().any()
        or frame.duplicated(["date", "code"]).any()
        or not frame["code"].str.fullmatch(r"[0-9]{6}").all()
        or frame[prices].isin([float("inf"), float("-inf")]).any().any()
        or (frame[["open", "close"]] <= 0).any().any()
        or (frame["amount"] < 0).any()
    ):
        raise ValueError("请检查空值、重复日期和代码、六位股票代码、正价格与非负成交额。")
    return frame


@st.cache_resource
def package_downloader():
    path = Path(__file__).with_name("SKILL.md")
    source = path.read_text(encoding="utf-8")
    namespace = {}
    for marker in ("v39-helpers", "v39-tdx-package"):
        pattern = rf"<!-- {marker}:start -->\s*```python\n(.*?)\n```"
        code = re.search(pattern, source, re.S)
        if code is None:
            raise RuntimeError(f"SKILL.md 中缺少 {marker} 代码块。")
        exec(compile(code[1], str(path), "exec"), namespace)
    return namespace["tdx_daily_package"]


MARKET_CACHE = Path(__file__).resolve().with_name(".market-data")
MAX_PACKAGES = 180


def package_path(day):
    return MARKET_CACHE / f"{day}.csv.gz"


def daily_package(day):
    path = package_path(day)
    try:
        return pd.read_csv(path, dtype={"code": str}, compression="gzip")
    except FileNotFoundError:
        pass

    snapshot = package_downloader()(day)
    MARKET_CACHE.mkdir(exist_ok=True)
    with NamedTemporaryFile(dir=MARKET_CACHE, suffix=".csv.gz", delete=False) as temporary:
        pending = Path(temporary.name)
    try:
        snapshot.to_csv(pending, index=False, compression="gzip")
        pending.replace(path)
    finally:
        pending.unlink(missing_ok=True)
    return snapshot


def update_packages(end, days, progress):
    available = []
    for index, day in enumerate(pd.bdate_range(end=end, periods=days)):
        key = day.date().isoformat()
        progress.progress((index + 1) / days, text=f"查询 {key}")
        if not package_path(key).is_file():
            try:
                daily_package(key)
            except ValueError as exc:
                if "没有通达信盘后包" not in str(exc):
                    raise
                continue
        available.append(key)
    if not available:
        raise ValueError("所选区间暂无盘后包。")
    return available


def prune_packages(keep):
    paths = sorted(MARKET_CACHE.glob("????-??-??.csv.gz"), reverse=True)
    protected = {package_path(day) for day in keep}
    others = [path for path in paths if path not in protected]
    for path in others[max(0, MAX_PACKAGES - len(protected)):]:
        path.unlink(missing_ok=True)


def cache_today(day):
    daily_package(day)
    prune_packages([day])


@st.fragment(run_every=2)
def refresh_today(now):
    pending = st.session_state.get("today_download")
    if pending is not None:
        if not pending.done():
            st.caption("正在后台检查当日盘后包…")
            return
        st.session_state.pop("today_download")
        try:
            pending.result()
        except (ValueError, RuntimeError, OSError) as exc:
            if "没有通达信盘后包" not in str(exc):
                st.warning(f"当日行情自动更新失败：{exc}")
        else:
            st.rerun()
        return
    if now.weekday() >= 5 or now.hour < 18:
        return
    day = now.date().isoformat()
    attempt = (day, now.hour, now.minute // 15)
    if (
        package_path(day).is_file()
        or st.session_state.get("auto_refresh_attempt") == attempt
        or not next(MARKET_CACHE.glob("????-??-??.csv.gz"), None)
    ):
        return
    st.session_state["auto_refresh_attempt"] = attempt
    st.session_state["today_download"] = market_workers("tdx").submit(cache_today, day)
    st.caption("正在后台检查当日盘后包…")


def package_history(days):
    frames = [
        pd.read_csv(package_path(day), dtype={"code": str}, compression="gzip")
        for day in days
    ]
    data = pd.concat(frames, ignore_index=True)
    stock_prefixes = {"sh": r"60|68", "sz": r"00|30", "bj": r"43|83|87|88|92"}
    stock = pd.Series(False, index=data.index)
    for market, prefixes in stock_prefixes.items():
        stock |= data["market"].str.lower().eq(market) & data["code"].str.match(prefixes)
    data = data.loc[stock].sort_values(["code", "date"]).copy()
    if data.empty:
        raise ValueError("盘后包中没有匹配的股票。")
    data["date"] = pd.to_datetime(data["date"])
    # Chain each day's returns against the exchange's reference close.
    reference = data["prev_close"].where(data["prev_close"].gt(0))
    ratio = data["close"].div(reference)
    adjusted = ratio.fillna(1).groupby(data["code"]).cumprod()
    data["open"] = data["open"].div(data["close"]).mul(adjusted).where(data["open"].gt(0) & reference.notna())
    data["close"] = adjusted.where(reference.notna())
    return data



def ranks(close, amount, lookback, min_amount):
    average = amount.rolling(lookback).mean()
    momentum = close.pct_change(lookback, fill_method=None)
    valid = close.notna().rolling(lookback + 1).sum().eq(lookback + 1)
    return momentum.where(valid & average.ge(min_amount) & amount.gt(0)), average


def backtest(scores, opening, amount, start, end, count, fee, step):
    dates = scores.loc[start:end].index
    records = []
    for i in range(0, len(dates) - step - 1, step):
        signal, entry, exit_day = dates[i], dates[i + 1], dates[i + step + 1]
        selected = scores.loc[signal].dropna().nlargest(count).index
        if selected.empty:
            continue
        buy = opening.loc[entry, selected].where(amount.loc[entry, selected].gt(0))
        sell = opening.loc[exit_day, selected].where(amount.loc[exit_day, selected].gt(0))
        if buy.isna().any() or sell.isna().any():
            records.append((exit_day, float("nan"), len(selected)))
            continue
        net = ((sell / buy) * (1 - fee) / (1 + fee) - 1).mean()
        records.append((exit_day, net, len(selected)))
    return pd.DataFrame(records, columns=["退出日", "区间净收益", "入选数"]).set_index("退出日")


def cache_status():
    st.subheader("本地行情缓存")
    files = list(MARKET_CACHE.glob("????-??-??.csv.gz"))
    sizes = []
    for path in files:
        try:
            sizes.append(path.stat().st_size)
        except FileNotFoundError:
            continue
    st.write(f"已缓存 **{len(sizes)} / {MAX_PACKAGES}** 个交易日，占用 **{sum(sizes) / 1048576:.1f} MiB**。")
    st.code(str(MARKET_CACHE), language=None)
    st.caption("每天一个压缩 CSV。更新行情或查询盘后包时触发清理，优先保留本次使用的日期。")
    st.subheader("通达信盘后包")
    package_day = st.date_input("盘后包日期")
    if st.button("查询盘后包"):
        cached = package_path(package_day.isoformat()).is_file()
        try:
            with st.spinner("正在读取本地缓存或补下载…"):
                snapshot = daily_package(package_day.isoformat())
                prune_packages([package_day.isoformat()])
            st.session_state["inspected_daily_package"] = (package_day, snapshot, cached)
        except (ValueError, RuntimeError, OSError) as exc:
            st.error(f"盘后包查询失败：{exc}")
    inspected = st.session_state.get("inspected_daily_package")
    if inspected and inspected[0] == package_day:
        _, snapshot, cached = inspected
        st.write(f"{len(snapshot):,} 条记录 · " + ("本地缓存" if cached else "已下载并缓存"))
        st.dataframe(snapshot, column_config={"code": stock_code_button("copy_package_code", "代码")})
        copy_clicked_stock_code("copy_package_code")
        st.caption("盘后包为单日原始行情。研究页使用连续复权日线。")



st.set_page_config(page_title="A 股研究工作台", layout="wide", initial_sidebar_state="expanded")
st.html(Path(__file__).with_name("ui-zh.html"), unsafe_allow_javascript=True)
st.html(Path(__file__).with_name("dashboard.css"))
st.title("A 股研究工作台", anchor=False)
with st.sidebar:
    st.markdown("### 工作台导航")
    page = st.radio(
        "工作台导航",
        ["国际行情", "A股行情", "我的持仓", "选股与回测"],
        index=1,
        label_visibility="collapsed",
        key="workspace_navigation",
        bind="query-params",
    )
if page == "国际行情":
    international_view()
    st.stop()
if page == "A股行情":
    market_view()
    st.stop()
if page == "我的持仓":
    portfolio_view()
    st.divider()
    cached_days = sorted(path.name[:10] for path in MARKET_CACHE.glob("????-??-??.csv.gz"))[-30:]
    try:
        history = package_history(cached_days) if cached_days else pd.DataFrame()
        swing_view(history, [row[0] for row in positions()])
    except (OSError, ValueError, RuntimeError) as exc:
        st.error(f"波段数据读取失败：{exc}")
    st.stop()
with st.sidebar:
    st.header("研究设置")
    mode = st.radio("数据来源", ["通达信盘后行情", "上传 CSV", "模拟数据"])
    uploaded = None
    if mode == "通达信盘后行情":
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        end_day = st.date_input("行情截止日期", value=now.date())
        days = st.slider("行情范围（工作日）", 30, 90, 35)
        st.caption("优先读取磁盘缓存。已有缓存时，北京时间工作日 18:00 后打开或刷新页面会检查当日盘后包。")
        if end_day == now.date():
            refresh_today(now)
        if st.button("更新真实行情", type="primary"):
            progress = st.progress(0)
            available = None
            try:
                available = update_packages(end_day, days, progress)
                st.success(f"所选范围内已就绪 {len(available)} 个交易日。")
            except (ValueError, RuntimeError, OSError) as exc:
                st.error(f"行情更新失败：{exc}")
            finally:
                prune_packages(available if available is not None else [])
                progress.empty()
    if mode == "上传 CSV":
        uploaded = st.file_uploader("导入股票日线 CSV", type="csv")
    st.caption("字段 date、code、open、close、amount，可选 name（股票名称）和 sector（所属板块）。开盘和收盘价须采用一致的复权口径，成交额单位为元。")
    lookback = st.slider("动量窗口（交易日）", 5, 60, 20)
    min_amount = st.number_input("日均成交额下限（万元）", min_value=0, value=5000, step=1000)
    count = st.slider("候选数量", 1, 30, 10)

try:
    if mode == "模拟数据":
        data = demo_data()
    elif mode == "上传 CSV":
        if uploaded is None:
            st.info("请从侧栏上传复权日线 CSV。")
            st.stop()
        data = read_history(uploaded)
    else:
        available = [
            day.date().isoformat() for day in pd.bdate_range(end=end_day, periods=days)
            if package_path(day.date().isoformat()).is_file()
        ]
        if not available:
            selection, research, status = st.tabs(["每日选股", "策略回测", "数据状态"])
            with selection:
                st.info("所选范围暂无本地行情。点击左侧「更新真实行情」补下载。")
            with research:
                st.info("补齐所选范围的行情后即可回测。")
            with status:
                cache_status()
            st.stop()
        data = package_history(available)
except (ValueError, TypeError, OSError) as exc:
    st.error(f"读取行情失败：{exc}")
    st.stop()

if uploaded:
    st.caption(f"研究数据：{uploaded.name}")
elif mode == "模拟数据":
    st.info("当前使用模拟日线。")
else:
    st.caption("数据来源：通达信盘后包（本地磁盘缓存）。收益按盘后包昨收参考价串接，价格显示为归一化序列。只使用所选范围内已缓存的日期。")

close = data.pivot(index="date", columns="code", values="close").sort_index()
opening = data.pivot(index="date", columns="code", values="open").reindex_like(close)
amount = data.pivot(index="date", columns="code", values="amount").reindex_like(close)
scores, average = ranks(close, amount, lookback, min_amount * 10000)

selection, research, status = st.tabs(["每日选股", "策略回测", "数据状态"])
with selection:
    day = st.selectbox("选股日期", close.index[::-1], format_func=lambda value: value.strftime("%Y-%m-%d"))
    top = scores.loc[day].dropna().nlargest(count)
    if top.empty:
        st.info("当天没有符合条件的股票，请检查历史长度或成交额门槛。")
    else:
        selected_day = data.loc[data["date"].eq(day)].set_index("code")
        names = selected_day.get("name")
        stock_names = names.reindex(top.index).fillna("—") if names is not None else "—"
        sectors = selected_day.get("sector")
        stock_sectors = sectors.reindex(top.index).fillna("—") if sectors is not None else "—"
        result = pd.DataFrame({
            "股票名称": stock_names,
            "所属板块": stock_sectors,
            "动量收益 (%)": top * 100,
            "日均成交额 (万元)": average.loc[day, top.index] / 10000,
            ("归一化收盘" if mode == "通达信盘后行情" else "收盘价"): close.loc[day, top.index],
        })
        result.index.name = "股票代码"
        st.caption(f"过去 {lookback} 个交易日日均成交额达标，按同期复权收益率从高到低排名。")
        selection_table(result, mode != "模拟数据" and result["所属板块"].eq("—").any())

with research:
    st.caption("收盘排名，下一日开盘买入，固定间隔后开盘卖出。每期等权，按全进全出计费。")
    start = st.selectbox("起始日期", close.index, format_func=lambda value: value.strftime("%Y-%m-%d"))
    end = st.selectbox("截止日期", close.index[::-1], format_func=lambda value: value.strftime("%Y-%m-%d"))
    step = st.slider("持有间隔（交易日）", 1, 20, 5)
    fee = st.number_input("单边交易成本（基点）", 0, 500, 15)
    if st.button("运行回测", type="primary"):
        results = backtest(scores, opening, amount, start, end, count, fee / 10000, step)
        st.caption("成交模型使用开盘价及日成交额筛查，未模拟涨跌停排队、冲击成本或实际可成交量。")
        if results.empty:
            st.info("区间内没有完整持有周期及有效选股信号。")
        elif results["区间净收益"].isna().any():
            st.warning("部分周期有缺失开盘价或零成交额，无法得到完整净值。")
            st.dataframe(results)
        else:
            equity = (1 + results["区间净收益"]).cumprod()
            drawdown = equity / equity.cummax().clip(lower=1) - 1
            left, middle, right = st.columns(3)
            left.metric("累计净收益", f"{equity.iloc[-1] - 1:.2%}")
            middle.metric("按调仓周期计算的最大回撤", f"{drawdown.min():.2%}")
            right.metric("完成周期", len(equity))
            st.line_chart(equity.rename("策略净值"))
            st.dataframe(results)

with status:
    left, middle, right = st.columns(3)
    left.metric("最后数据日", close.index[-1].strftime("%Y-%m-%d"))
    middle.metric("证券数量", len(close.columns))
    right.metric("日线记录", len(data))
    st.caption(f"输入包含 {len(close)} 个日期，日期与证券交叉表有 {int(close.isna().sum().sum())} 个空格。")
    cache_status()
