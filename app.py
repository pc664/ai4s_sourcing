"""AI4S 人才地图 · 可视化软件（Streamlit）

先运行 python run.py 生成数据，然后：
    streamlit run app.py
浏览器会自动打开 http://localhost:8501
"""
from __future__ import annotations

import difflib
from html import escape as html_escape
import math
import re
import unicodedata
from datetime import datetime
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import pycountry
import streamlit as st
import yaml

from src import store
from src import taxonomy as tx
from src import tracks
from src import ui
from src.venture import QUADRANTS, SEP
from src.store import CRM_COLS, OVERRIDABLE

ROOT = Path(__file__).parent
DATA = ROOT / "output" / "app_data"
LEGACY_CRM = ROOT / "crm.csv"    # 旧版跟进记录，首次启动时自动导入数据库 team.db
STATUSES = ["未接触", "待研究", "已接触", "已约见", "重点跟进", "已投/在谈", "放弃"]
CFG = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
TAX = CFG.get("taxonomy") or {}
RELATIONS = TAX.get("relation_levels", ["L1 共研/论文合作", "L2 有接触", "L3 未接触"])
INTENTS = TAX.get("intent_levels", ["已创业", "有意向", "不确定"])
VERIFY = ["待确认", "已核实"]
TIERS = list(dict.fromkeys(r["name"] for r in TAX.get("tiers", [])))
DOMAINS = tx.domain_order(TAX) + [tx.UNCLASSIFIED] if TAX else []
METHODS = tx.method_order(TAX) if TAX else []
PARENT = tx.domain_parent(TAX) if TAX else {}
AUTH = (CFG.get("auth") or {}).get("enabled", True)
CN = {"US": "美国", "CN": "中国", "GB": "英国", "DE": "德国", "CH": "瑞士", "FR": "法国",
      "JP": "日本", "KR": "韩国", "CA": "加拿大", "IL": "以色列", "NL": "荷兰", "SG": "新加坡",
      "AU": "澳大利亚", "IN": "印度", "SE": "瑞典", "DK": "丹麦", "ES": "西班牙", "IT": "意大利",
      "BE": "比利时", "AT": "奥地利", "HK": "中国香港", "TW": "中国台湾", "FI": "芬兰", "NO": "挪威"}

st.set_page_config(page_title="AI4S 人才地图", page_icon="🧬", layout="wide")
ui.inject()   # 深色终端风主题（样式和图表配色）


# ---------------------------------------------------------------- 登录
def login_gate() -> dict:
    if not AUTH:
        return {"username": "local", "name": "本地用户", "role": "admin"}
    if st.session_state.get("user"):
        return st.session_state["user"]
    _, mid, _ = st.columns([1, 1.2, 1])
    with mid:
        st.title("🧬 AI4S 人才地图")
        if store.user_count() == 0:
            st.warning("还没有任何账号。请在电脑终端进入软件文件夹，运行下面的命令创建第一个管理员，然后刷新本页：")
            st.code("python3 manage_users.py add 你的用户名 --name 你的姓名 --role admin")
            st.stop()
        with st.form("login"):
            u = st.text_input("用户名")
            pw = st.text_input("密码", type="password")
            ok = st.form_submit_button("登录", type="primary", width="stretch")
        if ok:
            user, err = store.verify(u, pw)
            if user:
                st.session_state["user"] = user
                st.rerun()
            st.error(err)
        st.caption("没有账号或忘记密码，请联系管理员。")
    st.stop()


USER = login_gate()
CAN_EDIT = USER["role"] in ("admin", "editor")
IS_ADMIN = USER["role"] == "admin"

# ---------------------------------------------------------------- 赛道（AI4S / 通用 AI …）
TRACKS = tracks.names(CFG)
with st.sidebar:
    st.title("🧬 AI4S 人才地图")
    if st.session_state.get("goto_track") in TRACKS:   # 要打开的人在另一个赛道：先切赛道
        st.session_state["track"] = st.session_state.pop("goto_track")
    if len(TRACKS) > 1:
        TRACK = st.radio("赛道", list(TRACKS), key="track", horizontal=True,
                         format_func=lambda k: TRACKS[k] + ("" if (tracks.app_dir(ROOT, k) / "talents.csv").exists()
                                                           else "（暂无数据）"))
    else:
        TRACK = tracks.DEFAULT
if st.session_state.get("_last_track") != TRACK:   # 换赛道时清掉和上个赛道相关的页面状态
    for k in list(st.session_state.keys()):
        if str(k).startswith(("star_", "radar_", "person_pick", "jump_", "net_", "co_", "fav_table", "fav_last_pick",
                              "nav_history", "focus_author")):
            st.session_state.pop(k, None)
    st.session_state["_last_track"] = TRACK
if "goto_focus" in st.session_state:                # 跳转目标（切赛道清理之后再放回去）
    st.session_state["focus_author"] = st.session_state.pop("goto_focus")
CFG_T = tracks.apply(CFG, TRACK)


@st.cache_data(max_entries=8)
def track_ids(track: str, version: float) -> set:
    """某个赛道人才库里的全部作者号（只读一列，很快）。"""
    return set(pd.read_csv(tracks.app_dir(ROOT, track) / "talents.csv", usecols=["author_id"])["author_id"])


def find_track(aid: str) -> str | None:
    """这个人在哪个赛道：优先当前赛道。"""
    for k in [TRACK] + [t for t in TRACKS if t != TRACK]:
        f_ = tracks.app_dir(ROOT, k) / "talents.csv"
        if f_.exists() and aid in track_ids(k, f_.stat().st_mtime):
            return k
    return None
TAX = CFG_T.get("taxonomy") or {}
DOMAINS = tx.domain_order(TAX) + [tx.UNCLASSIFIED] if TAX else []
METHODS = tx.method_order(TAX) if TAX else []
PARENT = tx.domain_parent(TAX) if TAX else {}
DATA = tracks.app_dir(ROOT, TRACK)
RUN_CMD = "python3 run.py" + ("" if TRACK == tracks.DEFAULT else f" --track {TRACK}")



# ---------------------------------------------------------------- 数据
@st.cache_data(max_entries=4)
def load(version: float = 0, folder: str = ""):
    """version = 数据文件的修改时间：每周自动更新数据后，软件下次刷新就会读到新数据，无需重启。"""
    t = pd.read_csv(DATA / "talents.csv", low_memory=False)
    p = pd.read_csv(DATA / "papers.csv")
    a = pd.read_csv(DATA / "authorships.csv", low_memory=False)
    i = pd.read_csv(DATA / "institutions.csv")
    p["子方向"] = p["子方向"].fillna("")
    t["主要方向"] = t["主要方向"].fillna("")
    t["国家/地区"] = t["国家/地区"].fillna("未知")
    t["国家"] = t["国家/地区"].map(country_cn)
    return t, p, a, i


def country_cn(code: str) -> str:
    if code in CN:
        return CN[code]
    c = pycountry.countries.get(alpha_2=str(code))
    return c.name if c else str(code)


def txt(v, default="—") -> str:
    return default if v is None or (isinstance(v, float) and math.isnan(v)) or v == "nan" else str(v)


def iso3(code: str):
    c = pycountry.countries.get(alpha_2=str(code))
    return c.alpha_3 if c else None


def norm_name(s) -> str:
    """姓名归一化：去掉声调符号、连字符、空格和大小写，"Xuan-He Zhou" → "xuanhezhou"。"""
    s = unicodedata.normalize("NFKD", str(s or ""))
    return re.sub(r"[^a-z]", "", "".join(c for c in s if not unicodedata.combining(c)).lower())


def name_variants(q: str) -> list[str]:
    """搜索词的几种写法：原顺序、姓在后/姓在前；中文名自动转拼音（需要 pypinyin）。"""
    q = (q or "").strip()
    if not q:
        return []
    if re.search(r"[\u4e00-\u9fff]", q):
        try:
            from pypinyin import lazy_pinyin
        except ImportError:
            return []
        toks = [t for t in lazy_pinyin(re.sub(r"[^\u4e00-\u9fff]", "", q)) if t]
        if not toks:
            return []
        surname, given = toks[0], "".join(toks[1:])   # 中文名：第一个字是姓
        return list(dict.fromkeys([given + surname, surname + given]))
    toks = [norm_name(t) for t in re.split(r"[\s,，]+", q) if norm_name(t)]
    if not toks:
        return []
    out = ["".join(toks)]
    if len(toks) >= 2:
        out += ["".join(toks[1:] + toks[:1]), "".join(toks[-1:] + toks[:-1])]
    return list(dict.fromkeys(out))


def name_label(name) -> str:
    """下拉框里的名字：带连字符或特殊字母的，后面附一个普通写法，方便在下拉框里直接打字搜索。
    例如 "Xuan-He Zhou（Xuanhe Zhou）"、"Augustin Žídek（Augustin Zidek）"。"""
    name = str(name or "")
    plain = unicodedata.normalize("NFKD", name)
    plain = "".join(c for c in plain if not unicodedata.combining(c))
    plain = re.sub(r"(\w)-(\w)", lambda m: m.group(1) + m.group(2).lower(), plain)
    return name if plain == name else f"{name}（{plain}）"


def profile_link(aid: str) -> str:
    """S2 人员（S2-xxx）链接到 Semantic Scholar，其余链接到 OpenAlex。"""
    if str(aid).startswith("S2-"):
        return f"[Semantic Scholar](https://www.semanticscholar.org/author/{str(aid)[3:]})"
    return f"[OpenAlex](https://openalex.org/{aid})"


def load_aliases() -> dict:
    """旧作者号 → 新人员号（接入 Semantic Scholar 后，同一个人的作者号可能变了）。"""
    p = DATA / "id_aliases.csv"
    if not p.exists():
        return {}
    a = pd.read_csv(p, dtype=str)
    return dict(zip(a["old_id"], a["new_id"]))


def apply_crm(t: pd.DataFrame, crm: pd.DataFrame) -> pd.DataFrame:
    """把跟进记录和人工修正合并进人才表：人工值优先，自动值保留在「xx(自动)」列。"""
    t = t.merge(crm.drop(columns=["姓名", "更新时间", "更新人", "版本"]), on="author_id", how="left")
    t["状态"] = t["状态"].replace("", pd.NA).fillna("未接触")
    t["关系强度"] = t["关系强度"].replace("", pd.NA).fillna(RELATIONS[-1])
    t["转化意向"] = t["转化意向"].replace("", pd.NA).fillna(INTENTS[-1])
    t["核实状态"] = t["核实状态"].replace("", pd.NA).fillna(VERIFY[0])
    for c in OVERRIDABLE:
        t[f"{c}(自动)"] = t[c]
        manual = t[f"{c}(人工)"].fillna("")
        t[c] = manual.where(manual != "", t[c])
    t["一级学科"] = t["主学科"].map(PARENT).fillna(tx.UNCLASSIFIED)
    for c in ["副学科", "副方法"]:
        t[c] = t[c].fillna("")
    return t


def tag_match(df: pd.DataFrame, main: str, sec: str, picked: list[str], with_sec: bool) -> pd.Series:
    m = df[main].isin(picked)
    return m | df[sec].isin(picked) if with_sec else m


def cell_members(df: pd.DataFrame, method: str, domain: str, level: str, with_sec: bool) -> pd.DataFrame:
    ms = tag_match(df, "主方法", "副方法", [method], with_sec)
    if level == "一级":
        d1 = df["主学科"].map(PARENT).fillna(tx.UNCLASSIFIED)
        d2 = df["副学科"].map(PARENT).fillna("")
        ds = (d1 == domain) | ((d2 == domain) if with_sec else False)
    else:
        ds = tag_match(df, "主学科", "副学科", [domain], with_sec)
    return df[ms & ds]


if not (DATA / "talents.csv").exists():
    st.error(f"「{TRACKS[TRACK]}」赛道还没有数据。请先在终端进入软件文件夹运行：`{RUN_CMD}`"
             f"（想先试跑几分钟看效果：`{RUN_CMD} --quick`），跑完后刷新本页。")
    st.stop()

T, P, A, I = load(max((DATA / f).stat().st_mtime for f in
                         ["talents.csv", "papers.csv", "authorships.csv", "institutions.csv"]), str(DATA))
if not TAX or "主学科" not in T.columns:
    st.warning(f"数据还是旧版，没有「科学领域 × AI方法」分类。请在终端运行：`{RUN_CMD} --skip-fetch`"
               "（复用已抓取的数据，几秒钟完成，不耗 API 额度），然后刷新本页。")
    st.stop()
if store.migrate_csv(LEGACY_CRM):
    st.toast("已把旧版 crm.csv 的跟进记录导入团队数据库")
crm = store.load_crm()
_alias, _ids = load_aliases(), set(T["author_id"])
if _alias:   # 以前按旧作者号记的跟进，对到新的人员号上（同一个人有多条时以最新的为准）
    crm = crm.assign(author_id=crm["author_id"].map(lambda a: a if a in _ids else _alias.get(a, a)))
    crm = crm.sort_values("更新时间").drop_duplicates("author_id", keep="last")
T = apply_crm(T, crm)

# 团队收藏（全组共享，存在 team.db）
FAV = store.list_favorites()
if _alias and len(FAV):
    FAV["author_id"] = FAV["author_id"].map(lambda a: a if a in _ids else _alias.get(a, a))
    FAV = FAV.drop_duplicates("author_id")   # 同一个人的多个旧作者号合并后只保留一条
FAV_IDS = set(FAV["author_id"])
T["⭐"] = T["author_id"].isin(FAV_IDS).map({True: "⭐", False: ""})
T = T.merge(FAV[["author_id", "分组"]].rename(columns={"分组": "收藏分组"}), on="author_id", how="left")
T["收藏分组"] = T["收藏分组"].fillna("")


def fav_widget(aid: str, name: str, key: str):
    """人名旁边的收藏按钮：未收藏时可加入（选分组、写理由），已收藏时可修改或取消。"""
    row = FAV[FAV["author_id"] == aid]
    groups = list(dict.fromkeys(store.FAV_GROUPS + [g for g in FAV["分组"] if g]))
    if len(row):
        r = row.iloc[0]
        label = f"⭐ 已收藏 · {r['分组'] or '未分组'}"
        st.caption(f"{r['收藏人']} 于 {r['收藏时间'][:10]} 收藏" + (f"：{r['理由']}" if r["理由"] else ""))
    else:
        label = "☆ 加入团队收藏"
    # 同名的人已经收藏过：多半是同一个人被拆成了多个档案，提醒一下，避免重复收藏
    same = FAV[(FAV["author_id"] != aid) & (FAV["姓名"].map(norm_name) == norm_name(name))] if not len(row) else FAV.iloc[0:0]
    if len(same):
        label = "☆ 加入团队收藏（已有同名收藏）"
    with st.popover(label, disabled=not CAN_EDIT, help=None if CAN_EDIT else "只读账号不能收藏"):
        if len(same):
            st.warning("团队收藏里已经有同名的人：" + "；".join(f"{r['理由'] or r['姓名']}" for _, r in same.iterrows())
                       + "。如果是同一个人（档案被拆开了），不用重复收藏；确认是不同的人再加入。")
        cur = row.iloc[0] if len(row) else {"分组": groups[0], "理由": ""}
        g = st.selectbox("分组", groups + ["＋ 新建分组…"], key=f"favg_{key}",
                         index=groups.index(cur["分组"]) if cur["分组"] in groups else 0)
        if g == "＋ 新建分组…":
            g = st.text_input("新分组名称", key=f"favng_{key}").strip() or groups[0]
        why = st.text_input("收藏理由（可选）", value=cur["理由"], key=f"favw_{key}",
                            placeholder="例如：Intern-S1 负责人，适合聊科学大模型方向")
        c1, c2 = st.columns(2)
        if c1.button("保存" if len(row) else ("仍然加入" if len(same) else "加入收藏"), key=f"favok_{key}",
                     type="primary", width="stretch"):
            store.add_favorite(aid, name, USER["name"], g, why)
            st.rerun()
        if len(row) and c2.button("取消收藏", key=f"favdel_{key}", width="stretch"):
            store.remove_favorite(aid, USER["name"])
            st.rerun()


SUBTOPICS = sorted({s for v in P["子方向"] for s in v.split("|") if s})

# ---------------------------------------------------------------- 侧边栏筛选
with st.sidebar:
    if AUTH:
        u1, u2 = st.columns([2, 1])
        u1.caption(f"👤 {USER['name']}（{store.ROLES.get(USER['role'], USER['role'])}）")
        if u2.button("退出", width="stretch"):
            st.session_state.pop("user", None)
            st.rerun()
    st.caption(f"范围：{CFG_T.get('track_name', '')} · {len(T):,} 位人才 · {len(P):,} 篇论文")
    st.divider()
    only_fav = st.toggle(f"⭐ 只看团队收藏（{len(FAV_IDS)}）", key="only_fav")
    st.markdown("**分类筛选**")
    f_top = st.multiselect("一级学科", list(TAX["science_domains"]) + [tx.UNCLASSIFIED])
    f_dom = st.multiselect("科学领域（二级）", [d for d in DOMAINS if not f_top or PARENT.get(d, tx.UNCLASSIFIED) in f_top])
    f_met = st.multiselect("AI方法", METHODS)
    with_sec = st.checkbox("学科 / 方法同时匹配副标签", help="不勾选时只按主学科、主方法筛选")
    f_type = st.multiselect("人才类型", tx.TALENT_TYPES)
    f_tier = st.multiselect("层级", TIERS)
    f_rel = st.multiselect("关系强度", RELATIONS)
    f_int = st.multiselect("转化意向", INTENTS)
    f_ver = st.multiselect("核实状态", VERIFY)
    with st.expander("更多筛选"):
        countries = st.multiselect("国家/地区", sorted(T["国家"].unique()))
        stages = st.multiselect("职业阶段", [s for s in ["博士生/博后", "博后末期/青年PI", "PI中期", "资深"]
                                         if s in set(T["职业阶段"])])
        topics = st.multiselect("蛋白子方向", SUBTOPICS)
        status = st.multiselect("跟进状态", STATUSES)
        min_score = st.slider("综合分不低于", 0, 100, 0)
        only_feeder = st.checkbox("只看创始人工厂出身")
        only_moved = st.checkbox("只看近 2 年跳槽的人")
        only_company = st.checkbox("只看当前在企业的人")
    kw = st.text_input("搜索姓名 / 机构", help="支持中文名（如 周煊赫）、不同拼写（Xuanhe Zhou / Xuan-He Zhou / Zhou Xuanhe）")

f = T.copy()
if only_fav:
    f = f[f["⭐"] == "⭐"]
if f_top:
    tops = f["一级学科"].isin(f_top)
    f = f[tops | f["副学科"].map(PARENT).isin(f_top)] if with_sec else f[tops]
if f_dom:
    f = f[tag_match(f, "主学科", "副学科", f_dom, with_sec)]
if f_met:
    f = f[tag_match(f, "主方法", "副方法", f_met, with_sec)]
for col, picked in [("人才类型", f_type), ("层级", f_tier), ("关系强度", f_rel),
                    ("转化意向", f_int), ("核实状态", f_ver)]:
    if picked:
        f = f[f[col].isin(picked)]
if countries:
    f = f[f["国家"].isin(countries)]
if stages:
    f = f[f["职业阶段"].isin(stages)]
if topics:
    f = f[f["主要方向"].apply(lambda s: any(t in s for t in topics))]
if status:
    f = f[f["状态"].isin(status)]
f = f[f["综合分"] >= min_score]
if only_feeder:
    f = f[f["出身创始人工厂"].astype(str).str.lower() == "true"]
if only_moved and "最近加入年份" in f:
    f = f[f["最近加入年份"].fillna(0) >= datetime.now().year - 2]
if only_company:
    f = f[f["当前机构类型"] == "company"]
if kw:
    keys = name_variants(kw)
    norm = f["姓名"].map(norm_name)
    m = norm.map(lambda n: any(k in n for k in keys)) if keys else pd.Series(False, index=f.index)
    m = m | f["当前机构"].str.contains(re.escape(kw.strip()), case=False, na=False)
    if not m.any() and keys:
        # 没有直接命中：按拼写相似度再找一遍（容忍少打、多打、打错一两个字母）
        uniq = set(norm)
        close = {c for k in keys for c in difflib.get_close_matches(k, uniq, n=10, cutoff=0.8)}
        m = norm.isin(close)
        if m.any():
            st.sidebar.caption(f"没有完全一致的名字，按相似拼写找到 {int(m.sum())} 位")
    f = f[m]

# 页面导航：用可以由程序切换的导航条代替标签页，点名字就能直接跳到「人才画像」；只渲染当前页，速度也更快
# ---------------------------------------------------------------- 顶部状态栏 & 快讯
_has_v = "成熟度分" in T.columns
_n_hot = int(T["创业象限"].astype(str).str.startswith("①").sum()) if _has_v else 0
_n_new = int((T["新增信号"].fillna("") != "").sum()) if _has_v and "新增信号" in T else 0
ui.top_bar(f"AI4S TALENT TERMINAL <b>//</b> {html_escape(CFG_T.get('track_name', ''))}",
           "Scientist sourcing · venture signal radar · early-stage",
           [("TALENTS", f"{len(T):,}", ""), ("NOW-TO-MEET", f"{_n_hot:,}", "r"),
            ("NEW SIGNALS", f"{_n_new:,}", "a"), ("WATCHLIST", f"{len(FAV_IDS)}", "g")],
           ui.now_text((DATA / "talents.csv").stat().st_mtime))
if _has_v:
    _feed = T[T["新增信号"].fillna("") != ""] if _n_new else T[T["创业象限"].astype(str).str.startswith("①")]
    _feed = _feed.assign(_s=_feed["成熟度分"] + _feed["潜力分"]).sort_values("_s", ascending=False).head(14)
    _col = "新增信号" if _n_new else "关键信号"
    ui.ticker([f"<b>{html_escape(r['姓名'])}</b> {html_escape(txt(r['当前机构'], ''))} · "
               f"<i>{html_escape(txt(r[_col], '').split(SEP)[0])[:90]}</i>" for _, r in _feed.iterrows()])

PAGES = {"radar": "🎯 创业雷达", "star": "🚀 新星榜", "fav": f"⭐ 团队收藏（{len(FAV_IDS)}）", "mx": "🧩 分类矩阵", "stat": "📊 分类统计",
         "map": "🌍 全球地图", "rank": "🏆 人才线索榜", "person": "👤 人才画像", "inst": "🏛️ 机构",
         "trend": "📈 方向趋势", "log": "📝 修改动态"}
if IS_ADMIN and AUTH:
    PAGES["admin"] = "👥 成员管理"
if st.session_state.get("goto_page") in PAGES:       # 程序要求跳页（必须在导航条画出来之前设置）
    st.session_state["page"] = st.session_state.pop("goto_page")
if st.session_state.get("page") not in PAGES:
    st.session_state["page"] = "radar"
PAGE = st.radio("页面", list(PAGES), key="page", horizontal=True, label_visibility="collapsed",
                format_func=lambda k: PAGES[k])
HAS_STAR = "新星分" in T.columns


def open_profile(aid: str, clear_key: str | None = None):
    """跳到「👤 人才画像」页并打开这个人（人在另一个赛道时自动切过去）；
    clear_key 是触发跳转的表格，清掉它的选中状态，回来时不会再自动跳。"""
    if clear_key:
        st.session_state.pop(clear_key, None)
    where = find_track(aid)
    if where is None:
        st.warning("这个人在所有赛道的数据里都找不到（可能是数据更新后作者号变了）。")
        return
    if where != TRACK:
        st.session_state["goto_track"] = where
    st.session_state["goto_focus"] = aid
    st.session_state["goto_page"] = "person"
    st.rerun()


def picked_row(ev) -> int | None:
    rows = ((ev or {}).get("selection") or {}).get("rows", [])
    return rows[0] if rows else None

LIST_COLS = ["综合分", "姓名", "人才类型", "层级", "关系强度", "转化意向", "当前机构", "国家",
             "主学科", "副学科", "主方法", "副方法", "推荐理由"]
SCORE_CFG = {"综合分": st.column_config.ProgressColumn("综合分", min_value=0, max_value=100, format="%.0f"),
             "推荐理由": st.column_config.TextColumn(width="large")}

# ---------------------------------------------------------------- 新星榜
STAR_CFG = {
    "新星分": st.column_config.ProgressColumn("新星分", min_value=0, max_value=100, format="%.0f"),
    "影响力分": st.column_config.ProgressColumn("影响力", min_value=0, max_value=100, format="%.0f"),
    "势头分": st.column_config.ProgressColumn("势头", min_value=0, max_value=100, format="%.0f"),
    "独立性分": st.column_config.ProgressColumn("独立性", min_value=0, max_value=100, format="%.0f"),
    "创业信号分": st.column_config.ProgressColumn("创业信号", min_value=0, max_value=100, format="%.0f"),
    "学术年龄": st.column_config.NumberColumn(format="%d 年"),
    "新星理由": st.column_config.TextColumn(width="large"),
}
STAR_COLS = ["领域排名", "新星分", "⭐", "姓名", "主学科", "当前机构", "国家", "学术年龄", "层级", "人才类型", "主方法",
             "影响力分", "势头分", "独立性分", "创业信号分", "新星理由", "近期最佳论文", "关系强度", "状态"]

if PAGE == "radar":
    if "成熟度分" not in T.columns:
        st.warning(f"数据还没有创业信号评分。请在终端运行 `{RUN_CMD}`（会顺带补全作者档案；只想快速重算可加 `--skip-fetch`），然后刷新本页。")
    else:
        rd = f[f["创业象限"].fillna("") != ""].copy()
        qn = rd["创业象限"].value_counts()
        ui.hud_cards([
            ("Now to meet · 现在就该见", f"{int(qn.get(QUADRANTS[0], 0)):,}", "成熟度和潜力都在前 20%", ui.RED),
            ("Track · 技术好继续跟踪", f"{int(qn.get(QUADRANTS[1], 0)):,}", "潜力高、时机未到", ui.CYAN),
            ("Ready · 想创业待验证", f"{int(qn.get(QUADRANTS[2], 0)):,}", "时机到了、技术待看", ui.AMBER),
            ("New signals · 本周新信号", f"{int((rd['新增信号'].fillna('') != '').sum()) if '新增信号' in rd else 0:,}",
             "和上次数据更新相比", ui.NEON),
            ("Scored · 参与评分", f"{len(rd):,}", "近 3 年活跃的学者", "#94A3B8"),
        ])
        _dom = rd["主学科"].fillna("未归类").value_counts().head(7)
        _hot = rd[rd["创业象限"] == QUADRANTS[0]]["主学科"].value_counts()
        ui.globe(rd,
                 left=[(d, f"{int(_hot.get(d, 0))} / {n:,}", ui.SEQ[i % len(ui.SEQ)]) for i, (d, n) in enumerate(_dom.items())],
                 right=[("NOW TO MEET", f"{int(qn.get(QUADRANTS[0], 0)):,}", ui.RED),
                        ("TRACK", f"{int(qn.get(QUADRANTS[1], 0)):,}", ui.CYAN),
                        ("READY", f"{int(qn.get(QUADRANTS[2], 0)):,}", ui.AMBER),
                        ("AVG READINESS", f"{rd['成熟度分'].mean():.0f}", ui.NEON),
                        ("AVG POTENTIAL", f"{rd['潜力分'].mean():.0f}", ui.VIOLET),
                        ("WATCHLIST", f"{int(rd['author_id'].isin(FAV_IDS).sum())}", "#F472B6")],
                 title="TALENT SPHERE · 红=现在就该见 · 青=技术好继续跟踪 · 黄=想创业待验证")
        c1, c2, c3 = st.columns([2.2, 1.2, 1.2])
        qs = c1.multiselect("象限", QUADRANTS, default=[QUADRANTS[0]], key="radar_q")
        hide_done = c2.toggle("隐藏已创业 / 已投", value=True, key="radar_hide",
                              help="按跟进记录：转化意向为「已创业」，或跟进状态为「已投/在谈」的人不显示")
        only_new = c3.toggle("只看本周新信号", key="radar_new", help="和上一次数据更新相比，新出现信号的人")
        if hide_done:
            rd = rd[~((rd["转化意向"] == "已创业") | (rd["状态"] == "已投/在谈"))]
        if only_new and "新增信号" in rd:
            rd = rd[rd["新增信号"].fillna("") != ""]

        # 四象限图
        cut = (CFG_T.get("venture") or {}).get("quadrant_cut", 80)
        pts = rd.copy()
        pts["象限"] = pts["创业象限"]
        fig = px.scatter(pts, x="成熟度分", y="潜力分", color="象限", hover_name="姓名",
                         hover_data={"当前机构": True, "主学科": True, "成熟度分": ":.0f", "潜力分": ":.0f", "象限": False},
                         category_orders={"象限": QUADRANTS}, opacity=0.55, render_mode="webgl",
                         color_discrete_sequence=[ui.RED, ui.CYAN, ui.AMBER, "#3B4A63"])
        fig.add_vline(x=cut, line_dash="dot", line_color="#888")
        fig.add_hline(y=cut, line_dash="dot", line_color="#888")
        for x, y, t in [(99, 101, "现在就该见"), (cut / 2, 101, "技术好，继续跟踪"), (99, cut - 4, "想创业，技术待验证")]:
            fig.add_annotation(x=x, y=y, text=t, showarrow=False, xanchor="right" if x > 50 else "center",
                               font=dict(size=12, color="#666"))
        fig.update_layout(height=520, margin=dict(l=0, r=0, t=20, b=0), legend=dict(orientation="h", y=-0.12),
                          xaxis=dict(title="创业成熟度（时机）", range=[0, 103]),
                          yaxis=dict(title="创业潜力（价值）", range=[0, 105]))
        st.plotly_chart(fig, width="stretch")

        # 本周新信号
        if "新增信号" in rd and (rd["新增信号"].fillna("") != "").any() and not only_new:
            new = rd[rd["新增信号"].fillna("") != ""].sort_values("成熟度变化", ascending=False)
            with st.expander(f"🔔 本周新出现信号的人（{len(new)} 位）", expanded=True):
                for _, r in new.head(30).iterrows():
                    a1, a2 = st.columns([1.6, 5])
                    if a1.button(name_label(r["姓名"]), key=f"radar_new_{r['author_id']}", type="tertiary"):
                        open_profile(r["author_id"])
                    a2.caption(f"{txt(r['当前机构'], '')} · 成熟度 {r['成熟度分']:.0f}"
                               + (f"（{r['成熟度变化']:+.0f}）" if pd.notna(r.get("成熟度变化")) else "")
                               + f" · 潜力 {r['潜力分']:.0f} ｜ 新信号：" + r["新增信号"])

        # 名单
        rd = rd[rd["创业象限"].isin(qs)] if qs else rd
        rd = rd.assign(_s=rd["成熟度分"] + rd["潜力分"]).sort_values("_s", ascending=False)
        st.markdown(f"#### {'、'.join(q[2:] for q in qs) if qs else '全部'}（{len(rd)} 位）")
        st.caption("👉 点这一行最左边的小方框，即可打开他的人才画像；下面「快速查看」可以看完整信号清单")
        rcols = [c for c in ["创业象限", "成熟度分", "潜力分", "⭐", "姓名", "关键信号", "当前机构", "国家", "主学科",
                             "学术年龄", "关系强度", "状态"] if c in rd.columns]
        rcfg = {"成熟度分": st.column_config.ProgressColumn("成熟度", min_value=0, max_value=100, format="%.0f"),
                "潜力分": st.column_config.ProgressColumn("潜力", min_value=0, max_value=100, format="%.0f"),
                "关键信号": st.column_config.TextColumn(width="large"),
                "学术年龄": st.column_config.NumberColumn(format="%d 年")}
        rv = rd.head(500)
        rev2 = st.dataframe(rv[rcols], width="stretch", hide_index=True, height=min(650, 38 + 35 * max(1, len(rv))),
                            column_config=rcfg, on_select="rerun", selection_mode="single-row", key="radar_table")
        ri2 = picked_row(rev2)
        if ri2 is not None:
            open_profile(rv.iloc[ri2]["author_id"], "radar_table")
        if len(rd) > 500:
            st.caption(f"只显示前 500 位，完整名单请下载。")
        st.download_button("下载名单（CSV，含完整信号清单）",
                           rd[rcols + [c for c in ["信号清单", "新增信号", "ORCID", "author_id"] if c in rd]]
                           .to_csv(index=False).encode("utf-8-sig"), "创业雷达.csv", "text/csv")

        if len(rv):
            st.markdown("#### 快速查看")
            who = st.selectbox("选择一位", rv["author_id"].tolist(), key="radar_pick",
                               format_func=lambda a: (lambda r: f"{name_label(r['姓名'])} · {txt(r['当前机构'], '')}"
                                                      f" · 成熟度 {r['成熟度分']:.0f} / 潜力 {r['潜力分']:.0f}")(
                                   rv.set_index("author_id").loc[a]))
            r = rv.set_index("author_id").loc[who]
            v1, v2 = st.columns([3, 2])
            with v1:
                st.markdown(f"**{r['姓名']}** · {txt(r['当前机构'])} · {r['国家']} · {r['创业象限']}")
                st.markdown("**信号清单**（越新越靠前）")
                for item in txt(r.get("信号清单"), "").split(SEP):
                    if item:
                        st.markdown(f"- {item}")
                fav_widget(who, r["姓名"], f"radar_{who}")
            with v2:
                parts_r = ["职业转折", "独立带队", "核心团队", "产业合作上升", "系统化产出"]
                parts_p = ["成果质量", "离产品距离", "赛道窗口期", "技术壁垒"]
                bars = pd.DataFrame({"项目": parts_r + parts_p,
                                     "得分": [r.get(k) for k in parts_r + parts_p],
                                     "维度": ["成熟度"] * len(parts_r) + ["潜力"] * len(parts_p)})
                st.plotly_chart(px.bar(bars, x="得分", y="项目", color="维度", orientation="h", range_x=[0, 100],
                                       color_discrete_sequence=[ui.RED, ui.CYAN])
                                .update_layout(height=330, margin=dict(l=0, r=0, t=10, b=0),
                                               yaxis=dict(autorange="reversed", title=None), legend=dict(orientation="h")),
                                width="stretch")
                if st.button("打开人才画像（可记录跟进）", key="radar_open", type="primary"):
                    open_profile(who)

# ---------------------------------------------------------------- 新星榜
if PAGE == "star":
    if not HAS_STAR:
        st.warning(f"数据还没有新星评分。请在终端运行 `{RUN_CMD} --skip-fetch`（几秒到一分钟，不耗 API 额度），然后刷新本页。")
    else:
        stars = f[f["新星资格"].astype(str).str.lower() == "true"].copy()
        c1, c2, c3, c4 = st.columns([1, 2.2, 1.2, 1.2])
        s_level = c1.radio("领域粒度", ["二级", "一级"], horizontal=True, key="star_level")
        dcol = "主学科" if s_level == "二级" else "一级学科"
        order = DOMAINS if s_level == "二级" else list(TAX["science_domains"]) + [tx.UNCLASSIFIED]
        counts = stars[dcol].value_counts()
        dom_opts = ["全部领域"] + [d for d in order if counts.get(d, 0)]
        s_dom = c2.selectbox("领域", dom_opts, key="star_dom",
                             format_func=lambda d: f"全部领域（{len(stars)} 人，各领域分别排名）" if d == "全部领域"
                             else f"{d}（{counts.get(d, 0)} 人）")
        s_new = c3.checkbox("只看还没接触的", help="关系强度为 L3 未接触", key="star_new")
        s_sec = c4.checkbox("计入副学科", help="副学科是这个领域的新星也一起排名", key="star_sec")
        if s_new:
            stars = stars[stars["关系强度"].astype(str).str.startswith("L3")]

        if s_dom == "全部领域":
            stars = stars.sort_values("新星分", ascending=False)
            stars["领域排名"] = stars.groupby(dcol).cumcount() + 1
            top_k = st.slider("每个领域显示前几名", 3, 20, 5, key="star_topk")
            rows = []
            for d in order:
                g = stars[stars[dcol] == d].head(top_k)
                if len(g):
                    rows.append({"领域": f"{d}（{int((stars[dcol] == d).sum())}）",
                                 **{f"第{i + 1}名": f"{r['姓名']} · {txt(r['当前机构'], '')}"[:60]
                                    for i, (_, r) in enumerate(g.iterrows())}})
            st.markdown("#### 各领域新星总览")
            st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
            sview = stars[stars["领域排名"] <= top_k].sort_values([dcol, "领域排名"],
                                                               key=lambda s: s.map({d: i for i, d in enumerate(order)})
                                                               if s.name == dcol else s)
        else:
            in_dom = stars[dcol] == s_dom
            if s_sec:
                sec = stars["副学科"] if s_level == "二级" else stars["副学科"].map(PARENT)
                in_dom |= sec == s_dom
            sview = stars[in_dom].sort_values("新星分", ascending=False)
            sview["领域排名"] = range(1, len(sview) + 1)
            top_n = st.slider("显示前几名", 10, max(10, min(200, len(sview))), min(30, max(10, len(sview))),
                              key="star_topn") if len(sview) > 10 else len(sview)
            sview = sview.head(top_n)

        st.markdown(f"#### {'各领域新星排行' if s_dom == '全部领域' else s_dom + ' · 新星排行'}（{len(sview)} 人）")
        show = [c for c in STAR_COLS if c in sview.columns]
        st.caption("👉 点这一行最左边的小方框，即可打开他的人才画像")
        sev = st.dataframe(sview[show], width="stretch", hide_index=True, height=min(700, 38 + 35 * len(sview)),
                           column_config=STAR_CFG, on_select="rerun", selection_mode="single-row",
                           key=f"star_table_{s_dom}_{s_level}")
        si = picked_row(sev)
        if si is not None:
            open_profile(sview.iloc[si]["author_id"], f"star_table_{s_dom}_{s_level}")
        st.download_button("下载这份榜单（CSV）", sview[show + ["ORCID", "author_id"]].to_csv(index=False)
                           .encode("utf-8-sig"), f"新星榜_{s_dom}.csv", "text/csv")

        if len(sview):
            st.markdown("#### 快速查看")
            who = st.selectbox("选择一位新星", sview["author_id"].tolist(), key="star_pick",
                               format_func=lambda a: (lambda r: f"#{int(r['领域排名'])} {name_label(r['姓名'])} · "
                                                      f"{txt(r['当前机构'], '')}")(sview.set_index("author_id").loc[a]))
            r = sview.set_index("author_id").loc[who]
            q1, q2 = st.columns([3, 2])
            with q1:
                st.markdown(f"**{r['姓名']}** · {txt(r['当前机构'])} · {r['国家']} · 学术年龄 {txt(r['学术年龄'])} 年"
                            f" · {r['层级']} · {r['人才类型']}")
                st.markdown(f"`{r['主学科']}` `{r['主方法']}` `{r['关系强度']}` `{r['状态']}`")
                if txt(r.get("新星理由"), ""):
                    st.success(r["新星理由"])
                if txt(r.get("近期最佳论文"), ""):
                    pct = r.get("近期最佳论文百分位")
                    st.write(f"近期最佳论文：{r['近期最佳论文']}"
                             + (f"（同年同领域被引前 {max(1, round(100 - pct))}%）" if pd.notna(pct) else ""))
                links = [profile_link(who)]
                if isinstance(r.get("ORCID"), str):
                    links.append(f"[ORCID]({r['ORCID']})")
                links.append(f"[Google Scholar 搜索](https://scholar.google.com/scholar?q=%22{r['姓名'].replace(' ', '+')}%22)")
                st.markdown(" · ".join(links))
            with q2:
                k1, k2, k3, k4, k5 = st.columns(5)
                k1.metric("新星分", f"{r['新星分']:.0f}")
                k2.metric("影响力", f"{r['影响力分']:.0f}")
                k3.metric("势头", f"{r['势头分']:.0f}")
                k4.metric("独立性", f"{r['独立性分']:.0f}")
                k5.metric("创业信号", f"{r['创业信号分']:.0f}")
                fav_widget(who, r["姓名"], f"star_{who}")
                if st.button("打开人才画像（可记录跟进）", key="star_focus", type="primary"):
                    open_profile(who)

# ---------------------------------------------------------------- 团队收藏
if PAGE == "fav":
    if FAV.empty:
        st.info("还没有收藏。在「👤 人才画像」页或「🚀 新星榜」的快速查看里，点人名旁边的「☆ 加入团队收藏」即可收藏。"
                "收藏是全组共享的，每个人都能看到。")
    else:
        st.caption("全组共享的重点关注名单。在人才画像页或新星榜可以收藏 / 取消收藏；所有收藏变动都会记在「📝 修改动态」里。")
        g1, g2 = st.columns(2)
        fg = g1.multiselect("分组", sorted({g or "未分组" for g in FAV["分组"]}), key="fav_groups")
        fu = g2.multiselect("收藏人", sorted(set(FAV["收藏人"])), key="fav_users")
        cols = ["author_id", "姓名", "当前机构", "国家", "主学科", "主方法", "层级", "学术年龄", "综合分",
                "新星分", "关系强度", "状态", "负责人"]
        fv = FAV.merge(T[[c for c in cols if c in T.columns and c != "姓名"]], on="author_id", how="left")
        fv["分组"] = fv["分组"].replace("", "未分组")
        if fg:
            fv = fv[fv["分组"].isin(fg)]
        if fu:
            fv = fv[fv["收藏人"].isin(fu)]
        show = [c for c in ["分组", "姓名", "理由", "当前机构", "国家", "主学科", "层级", "学术年龄", "综合分", "新星分",
                            "关系强度", "状态", "负责人", "收藏人", "收藏时间"] if c in fv.columns]
        fv["所在赛道"] = [TRACKS.get(find_track(a) or "", "找不到") for a in fv["author_id"]]
        st.markdown(f"#### {len(fv)} 位")
        st.caption("👉 点名字直接打开他的人才画像；人在另一个赛道时会自动切换过去。")
        hdr = st.columns([2.2, 1.3, 3.2, 2.4, 1.6, 0.9, 1.2, 1.0])
        for c, t in zip(hdr, ["姓名", "分组", "理由", "当前机构", "主学科", "新星分", "关系强度", "所在赛道"]):
            c.markdown(f"**{t}**")
        for _, r in fv.iterrows():
            c = st.columns([2.2, 1.3, 3.2, 2.4, 1.6, 0.9, 1.2, 1.0], vertical_alignment="center")
            if c[0].button(name_label(r["姓名"]), key=f"favopen_{r['author_id']}", type="tertiary",
                           help="打开人才画像", disabled=r["所在赛道"] == "找不到"):
                open_profile(r["author_id"])
            c[1].write(r["分组"])
            c[2].caption(r["理由"] or "—")
            c[3].write(txt(r.get("当前机构"), "—"))
            c[4].write(txt(r.get("主学科"), "—"))
            c[5].write("—" if pd.isna(r.get("新星分")) else f"{r['新星分']:.0f}")
            c[6].write(txt(r.get("关系强度"), "—"))
            c[7].caption(r["所在赛道"])
        other = (fv["所在赛道"] != TRACKS[TRACK]).sum()
        if other:
            st.caption(f"有 {other} 位不在当前赛道（「{TRACKS[TRACK]}」），机构、领域等信息显示为 —，点名字会自动切到他所在的赛道。")
        st.download_button("下载收藏名单（CSV）", fv[show + ["所在赛道", "author_id"]].to_csv(index=False).encode("utf-8-sig"),
                           "团队收藏.csv", "text/csv")

# ---------------------------------------------------------------- 0. 分类矩阵
if PAGE == "mx":
    c1, c2, c3, c4 = st.columns([1, 1.4, 1, 1])
    level = c1.radio("科学领域粒度", ["二级", "一级"], horizontal=True)
    value = c2.radio("格子里显示", ["人数", "高潜人数", "交叉型人数", "已接触人数"], horizontal=True,
                     help="高潜：综合分≥70；交叉型：A×S；已接触：关系强度为 L1 或 L2")
    mx_sec = c3.checkbox("计入副标签", help="勾选后一个人可能同时出现在多个格子里")
    hide_empty = c4.checkbox("隐藏全空的列", value=True)

    M = tx.matrix(f, TAX, level, value, mx_sec)
    empty_cols = [c for c in M.columns if M[c].sum() == 0 and c != tx.UNCLASSIFIED]
    show_m = M.loc[:, M.sum() > 0] if hide_empty else M
    if show_m.shape[1] == 0:
        st.info("当前筛选条件下没有人才")
    else:
        z = show_m.values
        fig = go.Figure(go.Heatmap(
            z=z, x=list(show_m.columns), y=list(show_m.index), colorscale=[[0, "#0B1626"], [0.5, "#0E7490"], [1, ui.NEON]],
            text=[[str(v) if v else "" for v in row] for row in z], texttemplate="%{text}",
            hovertemplate="%{y} × %{x}<br>" + value + "：%{z}<extra></extra>", xgap=2, ygap=2))
        fig.update_layout(height=120 + 48 * len(show_m), margin=dict(l=0, r=0, t=10, b=0),
                          yaxis=dict(autorange="reversed"), xaxis=dict(side="top"))
        st.plotly_chart(fig, width="stretch")
    if empty_cols:
        st.caption("当前数据里完全空白的科学领域：" + "、".join(empty_cols))

    st.markdown("#### 查看某一格的人")
    nz = M.stack()
    nz = nz[nz > 0].sort_values(ascending=False)
    d_m, d_d = (nz.index[0] if len(nz) else (M.index[0], M.columns[0]))
    k1, k2 = st.columns(2)
    pick_m = k1.selectbox("AI方法", list(M.index), index=list(M.index).index(d_m))
    pick_d = k2.selectbox("科学领域", list(M.columns), index=list(M.columns).index(d_d))
    members = cell_members(f, pick_m, pick_d, level, mx_sec).sort_values("综合分", ascending=False)
    st.caption(f"{pick_m} × {pick_d}：{len(members)} 人（按综合分排序；指标筛选只影响热力图，这里列出全部）")
    st.dataframe(members[[c for c in LIST_COLS if c in members]], width="stretch", hide_index=True,
                 column_config=SCORE_CFG)

    with st.expander("名单矩阵（每格综合分前 5 名，可下载）"):
        rows = []
        for m in M.index:
            row = {"AI方法": m}
            for d in show_m.columns:
                mem = cell_members(f, m, d, level, mx_sec).nlargest(5, "综合分")
                row[d] = "、".join(mem["姓名"])
            rows.append(row)
        names = pd.DataFrame(rows)
        st.dataframe(names, width="stretch", hide_index=True)
        st.download_button("下载名单矩阵（CSV）", names.to_csv(index=False).encode("utf-8-sig"),
                           "人才地图_名单矩阵.csv", "text/csv")

# ---------------------------------------------------------------- 0.5 分类统计
if PAGE == "stat":
    s1, s2, s3, s4, s5 = st.columns(5)
    s1.metric("人才数（筛选后）", f"{len(f):,}")
    s2.metric("A×S 交叉型", int((f["人才类型"] == "A×S 交叉型").sum()))
    s3.metric("领军", int((f["层级"] == "领军").sum()) if "领军" in TIERS else 0)
    s4.metric("已接触（L1+L2）", int(f["关系强度"].str.startswith(("L1", "L2")).sum()))
    s5.metric("已创业 / 有意向", int(f["转化意向"].isin(INTENTS[:2]).sum()))

    def bar(col: str, order: list[str], title: str):
        vc = f[col].value_counts().reindex(order, fill_value=0).reset_index()
        vc.columns = [col, "人数"]
        return px.bar(vc, x="人数", y=col, orientation="h", title=title, text="人数") \
            .update_layout(height=280, yaxis=dict(autorange="reversed", title=None),
                           margin=dict(l=0, r=0, t=40, b=0))

    b1, b2 = st.columns(2)
    b1.plotly_chart(bar("人才类型", tx.TALENT_TYPES, "人才类型"), width="stretch")
    b2.plotly_chart(bar("层级", TIERS, "层级"), width="stretch")
    b3, b4 = st.columns(2)
    b3.plotly_chart(bar("关系强度", RELATIONS, "关系强度"), width="stretch")
    b4.plotly_chart(bar("转化意向", INTENTS, "转化意向"), width="stretch")

    x1, x2 = st.columns(2)
    with x1:
        st.markdown("#### 层级 × 人才类型")
        st.dataframe(pd.crosstab(f["层级"], f["人才类型"], margins=True, margins_name="合计")
                     .reindex(index=TIERS + ["合计"], columns=tx.TALENT_TYPES + ["合计"], fill_value=0),
                     width="stretch")
    with x2:
        st.markdown("#### 层级 × 关系强度")
        st.dataframe(pd.crosstab(f["层级"], f["关系强度"], margins=True, margins_name="合计")
                     .reindex(index=TIERS + ["合计"], columns=RELATIONS + ["合计"], fill_value=0),
                     width="stretch")

    st.markdown("#### 每个格子的人才结构（找空白和机会）")
    st.caption("覆盖率 = 已接触人数 / 人数。人多但覆盖率低的格子，是下一步重点触达的方向。")
    g = f.groupby(["主方法", "主学科"]).agg(
        人数=("author_id", "size"),
        交叉型=("人才类型", lambda s: int((s == "A×S 交叉型").sum())),
        领军=("层级", lambda s: int((s == "领军").sum())),
        高潜=("综合分", lambda s: int((s >= 70).sum())),
        已接触=("关系强度", lambda s: int(s.str.startswith(("L1", "L2")).sum())),
        有转化意向=("转化意向", lambda s: int(s.isin(INTENTS[:2]).sum())),
    ).reset_index()
    g["覆盖率"] = (g["已接触"] / g["人数"] * 100).round(0)
    g = g.sort_values("人数", ascending=False)
    st.dataframe(g, width="stretch", hide_index=True,
                 column_config={"覆盖率": st.column_config.ProgressColumn("覆盖率", min_value=0, max_value=100,
                                                                         format="%.0f%%")})
    st.download_button("下载格子统计（CSV）", g.to_csv(index=False).encode("utf-8-sig"),
                       "人才地图_格子统计.csv", "text/csv")

# ---------------------------------------------------------------- 1. 地图
if PAGE == "map":
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("人才数（筛选后）", f"{len(f):,}")
    c2.metric("覆盖国家/地区", f["国家"].nunique())
    c3.metric("高潜人才（综合分≥70）", int((f["综合分"] >= 70).sum()))
    c4.metric("AI4S 论文", f"{len(P):,}")

    metric = st.radio("地图显示", ["人才数", "高潜人才数", "平均综合分"], horizontal=True)
    g = f.groupby("国家/地区").agg(人才数=("author_id", "size"),
                                 高潜人才数=("综合分", lambda s: int((s >= 70).sum())),
                                 平均综合分=("综合分", "mean")).reset_index()
    g["iso3"] = g["国家/地区"].map(iso3)
    g["国家"] = g["国家/地区"].map(country_cn)
    g["平均综合分"] = g["平均综合分"].round(1)
    fig = px.choropleth(g.dropna(subset=["iso3"]), locations="iso3", color=metric,
                        hover_name="国家", hover_data={"iso3": False, "人才数": True,
                                                     "高潜人才数": True, "平均综合分": True},
                        color_continuous_scale=[[0, "#0B1626"], [0.5, "#0E7490"], [1, ui.NEON]])
    fig.update_layout(height=520, margin=dict(l=0, r=0, t=10, b=0),
                      geo=dict(showframe=False, projection_type="natural earth"))
    st.plotly_chart(fig, width="stretch")

    l, r = st.columns(2)
    top_c = g.sort_values("人才数", ascending=False).head(15)
    l.plotly_chart(px.bar(top_c, x="人才数", y="国家", orientation="h", title="人才数 Top 15")
                   .update_layout(yaxis=dict(autorange="reversed"), height=450), width="stretch")
    stage = f["职业阶段"].value_counts().reset_index()
    r.plotly_chart(px.pie(stage, names="职业阶段", values="count", hole=0.5, title="职业阶段分布")
                   .update_layout(height=450), width="stretch")

# ---------------------------------------------------------------- 2. 线索榜
if PAGE == "rank":
    st.caption(f"共 {len(f):,} 人 · 点击表头排序")
    show = ["综合分", "⭐", "姓名", "人才类型", "层级", "主学科", "主方法", "关系强度", "转化意向", "状态",
            "推荐理由", "当前机构", "国家", "职业阶段", "副学科", "副方法", "主要方向",
            "学术实力分", "增长势头分", "创业信号分", "赛道论文数", "近期论文数", "赛道总被引",
            "h指数", "最近加入机构", "最近加入年份", "代表作", "ORCID"]
    show = [c for c in show if c in f.columns]
    view = f[show].copy()
    score_col = lambda name: st.column_config.ProgressColumn(name, min_value=0, max_value=100, format="%.0f")
    st.caption("👉 点这一行最左边的小方框，即可打开他的人才画像")
    rev = st.dataframe(view, width="stretch", height=650, hide_index=True, on_select="rerun",
                       selection_mode="single-row", key="rank_table", column_config={
        "综合分": score_col("综合分"), "学术实力分": score_col("实力"),
        "增长势头分": score_col("势头"), "创业信号分": score_col("创业信号"),
        "推荐理由": st.column_config.TextColumn(width="large"),
        "ORCID": st.column_config.LinkColumn("ORCID"),
        "最近加入年份": st.column_config.NumberColumn(format="%d"),
    })
    ri = picked_row(rev)
    if ri is not None:
        open_profile(f.iloc[ri]["author_id"], "rank_table")
    st.download_button("下载当前筛选结果（CSV）", view.to_csv(index=False).encode("utf-8-sig"),
                       "人才线索_筛选.csv", "text/csv")

# ---------------------------------------------------------------- 3. 画像
if PAGE == "person":
    if f.empty:
        st.info("当前筛选条件下没有人才")
    else:
        # 要打开的人：从新星榜、合作网络或「返回」跳过来时，先写入选择框的状态，再画选择框
        pending = st.session_state.pop("focus_author", None)
        current = st.session_state.get("person_pick")
        in_pool = set(T["author_id"])
        opts = f.head(2000)
        extra = [a for a in (pending, current) if a and a in in_pool and a not in set(opts["author_id"])]
        if extra:   # 即使被左侧筛选条件挡住，也能打开
            opts = pd.concat([T[T["author_id"].isin(extra)], opts])
        ids = opts["author_id"].tolist()
        names = dict(zip(ids, opts["姓名"].map(name_label) + " · " + opts["当前机构"].fillna("未知机构")))
        if pending in names:
            st.session_state["person_pick"] = pending
        elif st.session_state.get("person_pick") not in names:
            st.session_state.pop("person_pick", None)
        aid = st.selectbox("选择人才（按综合分排序）", ids, key="person_pick", format_func=lambda a: names[a])
        p = opts[opts["author_id"] == aid].iloc[0]
        hist = st.session_state.setdefault("nav_history", [])
        if hist and hist[-1] != aid and hist[-1] in in_pool:
            prev = hist[-1]
            if st.button(f"← 返回 {T.loc[T['author_id'] == prev, '姓名'].iloc[0]}", key=f"back_{aid}"):
                hist.pop()
                st.session_state["focus_author"] = prev
                st.rerun()

        h1, h2 = st.columns([3, 2])
        with h1:
            st.subheader(("⭐ " if aid in FAV_IDS else "") + p["姓名"])
            fav_widget(aid, p["姓名"], f"person_{aid}")
            st.write(f"**{txt(p['当前机构'])}** · {p['国家']} · {txt(p['职业阶段'])}")
            tags = [p["人才类型"], p["层级"], f"{p['一级学科']} / {p['主学科']}", p["主方法"],
                    p["关系强度"], p["转化意向"], p["核实状态"]]
            st.markdown(" ".join(f"`{t}`" for t in tags if txt(t, "")))
            sec = [x for x in [p["副学科"], p["副方法"]] if x]
            if sec:
                st.caption("副标签：" + "、".join(sec))
            if txt(p["主要方向"], ""):
                st.write("蛋白子方向：" + p["主要方向"])
            if isinstance(p.get("推荐理由"), str) and p["推荐理由"]:
                st.success("推荐理由：" + p["推荐理由"])
            links = [profile_link(aid)]
            if isinstance(p.get("ORCID"), str):
                links.append(f"[ORCID]({p['ORCID']})")
            links.append(f"[Google Scholar 搜索](https://scholar.google.com/scholar?q=%22{p['姓名'].replace(' ', '+')}%22)")
            st.markdown(" · ".join(links))
        with h2:
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("综合", f"{p['综合分']:.0f}")
            m2.metric("实力", f"{p['学术实力分']:.0f}")
            m3.metric("势头", f"{p['增长势头分']:.0f}")
            m4.metric("信号", f"{p['创业信号分']:.0f}")
            n1, n2, n3 = st.columns(3)
            n1.metric("AI4S 论文", int(p["赛道论文数"]))
            n2.metric("AI4S 被引", f"{int(p['赛道总被引']):,}")
            hv = p.get("h指数")
            n3.metric("h 指数", "—" if pd.isna(hv) else int(hv))
            if HAS_STAR and str(p.get("新星资格")).lower() == "true":
                peers = T[(T["新星资格"].astype(str).str.lower() == "true") & (T["主学科"] == p["主学科"])]
                rank = int((peers["新星分"] > p["新星分"]).sum()) + 1
                st.info(f"🚀 学术新星 · 新星分 {p['新星分']:.0f} · {p['主学科']} 第 {rank} / {len(peers)} 名")

        mine = A[A["author_id"] == aid]
        works = P[P["work_id"].isin(mine["work_id"])].sort_values("年份")

        c1, c2 = st.columns(2)
        with c1:
            yearly = works.groupby("年份").agg(论文数=("work_id", "size"), 被引=("被引", "sum")).reset_index()
            fig = go.Figure()
            fig.add_bar(x=yearly["年份"], y=yearly["论文数"], name="论文数")
            fig.add_scatter(x=yearly["年份"], y=yearly["被引"], name="被引", yaxis="y2", mode="lines+markers")
            fig.update_layout(title="年度产出与被引", height=360,
                              yaxis2=dict(overlaying="y", side="right", showgrid=False),
                              legend=dict(orientation="h"))
            st.plotly_chart(fig, width="stretch")
        with c2:
            traj = mine.dropna(subset=["机构"]).fillna({"机构类型": "other"}).groupby(["年份", "机构", "机构类型"]).size().reset_index(name="篇数")
            fig = px.scatter(traj, x="年份", y="机构", size="篇数", color="机构类型", title="机构轨迹")
            fig.update_layout(height=360, yaxis_title=None, legend=dict(orientation="h"))
            st.plotly_chart(fig, width="stretch")

        # 合作网络（以本人为中心）：点击节点或下方表格中的一行，即可打开这位合作者的画像
        co = A[A["work_id"].isin(mine["work_id"]) & (A["author_id"] != aid)]
        co = (co.groupby(["author_id", "姓名"]).agg(合作次数=("work_id", "nunique"), 机构=("机构", "last"),
                                                   类型=("机构类型", "last"))
              .reset_index().sort_values("合作次数", ascending=False).head(30).reset_index(drop=True))
        pool = set(T["author_id"])

        def open_person(target: str):
            """跳到某位合作者：在人才库里就打开站内画像，否则给出外部主页链接。"""
            for k in (f"net_{aid}", f"co_{aid}"):   # 清掉这次点击，返回时不会再次自动跳转
                st.session_state.pop(k, None)
            if target in pool:
                st.session_state.setdefault("nav_history", []).append(aid)
                st.session_state["focus_author"] = target
                st.rerun()
            else:
                name = co.loc[co["author_id"] == target, "姓名"]
                st.info(f"{name.iloc[0] if len(name) else target} 的 AI4S 论文不足 2 篇，不在人才库里，"
                        f"可以打开外部主页查看：{profile_link(target)}")

        if len(co):
            n = len(co)
            co["x"] = [math.cos(2 * math.pi * k / n) for k in range(n)]
            co["y"] = [math.sin(2 * math.pi * k / n) for k in range(n)]
            score_map = T.set_index("author_id")["综合分"]
            co["综合分"] = co["author_id"].map(score_map)
            co["在人才库"] = co["author_id"].isin(pool)
            fig = go.Figure()
            for _, r in co.iterrows():
                fig.add_scatter(x=[0, r["x"]], y=[0, r["y"]], mode="lines", hoverinfo="skip",
                                line=dict(width=0.5 + r["合作次数"], color="rgba(34,211,238,0.25)"),
                                showlegend=False)
            colors = co["类型"].map({"company": ui.RED, "education": ui.CYAN}).fillna("#94A3B8")
            tip = co["在人才库"].map({True: "点击查看画像", False: "不在人才库，点击获取外部主页"})
            fig.add_scatter(x=co["x"], y=co["y"], mode="markers+text", text=co["姓名"],
                            textposition="top center", showlegend=False,
                            marker=dict(size=10 + 3 * co["合作次数"], color=colors,
                                        symbol=co["在人才库"].map({True: "circle", False: "circle-open"}),
                                        line=dict(width=2, color=colors)),
                            customdata=list(zip(co["机构"].fillna("—"), co["合作次数"],
                                                co["综合分"].round(0).fillna("—"), co["author_id"], tip)),
                            hovertemplate="<b>%{text}</b><br>%{customdata[0]}<br>合作 %{customdata[1]} 次"
                                          "<br>综合分 %{customdata[2]}<br><i>%{customdata[4]}</i><extra></extra>")
            fig.add_scatter(x=[0], y=[0], mode="markers+text", text=[p["姓名"]], textposition="bottom center",
                            marker=dict(size=28, color=ui.AMBER), showlegend=False, hoverinfo="skip")
            fig.update_layout(title="合作网络（点击节点打开画像 · 实心=在人才库，空心=不在 · 红=企业，蓝=高校 · 线越粗合作越多）",
                              height=560, clickmode="event+select", dragmode=False,
                              xaxis=dict(visible=False), yaxis=dict(visible=False, scaleanchor="x"))
            ev = st.plotly_chart(fig, width="stretch", on_select="rerun", selection_mode="points",
                                 key=f"net_{aid}")
            picked = [pt.get("customdata") for pt in ((ev or {}).get("selection") or {}).get("points", [])
                      if isinstance(pt.get("customdata"), (list, tuple)) and len(pt["customdata"]) >= 4]
            if picked:
                open_person(picked[0][3])

            # 不依赖图表点击的跳转方式（节点太密、或浏览器不响应点击时用）
            j1, j2 = st.columns([4, 1])
            jump = j1.selectbox("跳转到合作者", co["author_id"].tolist(), key=f"jump_{aid}",
                                format_func=lambda a: (lambda r: f"{name_label(r['姓名'])} · {txt(r['机构'], '')} · 合作 "
                                                       f"{int(r['合作次数'])} 次{'' if r['在人才库'] else ' · 不在人才库'}")(
                                    co.set_index("author_id").loc[a]))
            j2.write("")
            if j2.button("打开", key=f"jumpbtn_{aid}", width="stretch"):
                open_person(jump)

            with st.expander(f"合作者列表（{n} 人，点击一行打开画像）"):
                tbl = co[["姓名", "机构", "合作次数", "综合分", "在人才库"]].copy()
                tbl["外部主页"] = [("https://www.semanticscholar.org/author/" + a[3:]) if str(a).startswith("S2-")
                                else f"https://openalex.org/{a}" for a in co["author_id"]]
                ev2 = st.dataframe(tbl, width="stretch", hide_index=True, on_select="rerun",
                                   selection_mode="single-row", key=f"co_{aid}",
                                   column_config={"外部主页": st.column_config.LinkColumn("外部主页", display_text="打开"),
                                                  "综合分": st.column_config.NumberColumn(format="%.0f")})
                rows = ((ev2 or {}).get("selection") or {}).get("rows", [])
                if rows:
                    open_person(co.loc[rows[0], "author_id"])

        st.markdown("#### AI4S 论文")
        st.dataframe(works.sort_values("被引", ascending=False)[["年份", "标题", "被引", "期刊/平台", "子方向", "DOI"]],
                     width="stretch", hide_index=True,
                     column_config={"DOI": st.column_config.LinkColumn("DOI"),
                                    "年份": st.column_config.NumberColumn(format="%d")})

        if "成熟度分" in p and pd.notna(p.get("成熟度分")):
            with st.expander(f"🎯 创业信号：成熟度 {p['成熟度分']:.0f} · 潜力 {p['潜力分']:.0f} · {p['创业象限']}", expanded=True):
                for item in txt(p.get("信号清单"), "").split(SEP):
                    if item:
                        st.markdown(f"- {item}")
                if not txt(p.get("信号清单"), ""):
                    st.caption("暂时没有识别到具体的创业信号。")

        with st.expander("自动分类的依据"):
            st.write(f"**学科分布**（AI4S 论文篇数）：{txt(p.get('学科分布'))}")
            st.write(f"**方法分布**（AI4S 论文篇数）：{txt(p.get('方法分布'))}")
            share = p.get("AI侧占比")
            st.write(f"**AI侧占比**：{'—' if pd.isna(share) else f'{share:.0%}'}（依据：{txt(p.get('类型依据'))}）"
                     " → 人才类型。占比只来自 AI4S 论文时信号较弱，建议人工核实。")
            st.write(f"**层级**：学术年龄 {txt(p.get('学术年龄'))} 年，h 指数 {txt(p.get('h指数'))}")

        st.markdown("#### 跟进记录与人工修正")
        old = crm[crm["author_id"] == aid]
        o = old.iloc[0].to_dict() if len(old) else {c: "" for c in CRM_COLS}
        if o.get("更新时间"):
            st.caption(f"最近更新：{o['更新人']} · {o['更新时间']}")
        if not CAN_EDIT:
            st.info("你的账号是只读权限，可以查看但不能修改跟进记录。")

        def pick_opt(label, options, cur, default, col):
            return col.selectbox(label, options, index=options.index(cur) if cur in options else
                                 options.index(default))

        # 记住「打开表单时」看到的版本；保存时若数据库已被别人改过，就拒绝覆盖
        seen_key = f"seen_{aid}"
        seen = st.session_state.get(seen_key, int(o.get("版本") or 0))
        submitted = False
        with st.form(f"crm_{aid}"):
            s1, s2, s3, s4 = st.columns(4)
            new_status = pick_opt("跟进状态", STATUSES, o["状态"], "未接触", s1)
            rel = pick_opt("关系强度", RELATIONS, o["关系强度"], RELATIONS[-1], s2)
            intent = pick_opt("转化意向", INTENTS, o["转化意向"], INTENTS[-1], s3)
            ver = pick_opt("核实状态", VERIFY, o["核实状态"], VERIFY[0], s4)
            st.caption("自动分类不准时在这里修正；选「自动：…」表示沿用系统判定。")
            choices = {"人才类型": tx.TALENT_TYPES, "层级": TIERS, "主学科": DOMAINS, "主方法": METHODS}
            ov = {}
            for col, c in zip(st.columns(4), OVERRIDABLE):
                auto = f"自动：{p[f'{c}(自动)']}"
                opts = [auto] + choices[c]
                cur = o.get(f"{c}(人工)", "")
                v = col.selectbox(c, opts, index=opts.index(cur) if cur in opts else 0)
                ov[f"{c}(人工)"] = "" if v == auto else v
            owner = st.text_input("负责人", o["负责人"] or (USER["name"] if CAN_EDIT else ""))
            note = st.text_area("备注", o["备注"], placeholder="例如：已通过 XX 引荐，下周约电话；关注其新预印本…")
            submitted = st.form_submit_button("保存", disabled=not CAN_EDIT, type="primary")
            if submitted:
                try:
                    n = store.save_crm({"author_id": aid, "姓名": p["姓名"], "状态": new_status, "关系强度": rel,
                                        "转化意向": intent, "核实状态": ver, **ov, "负责人": owner, "备注": note},
                                       user=USER["name"], expected_version=seen)
                except store.Conflict as e:
                    st.error(f"没有保存：{e}。请刷新页面看到最新内容后再改。")
                else:
                    st.session_state.pop(seen_key, None)
                    st.success(f"已保存（{n} 项有变化）" if n else "没有变化")
                    st.rerun()
        if not submitted:
            st.session_state[seen_key] = int(o.get("版本") or 0)

        hist = store.crm_history(aid, limit=100)
        if len(hist):
            with st.expander(f"修改历史（{len(hist)} 条）"):
                st.dataframe(hist.drop(columns=["姓名"]), width="stretch", hide_index=True)

# ---------------------------------------------------------------- 4. 机构
if PAGE == "inst":
    kind = st.radio("机构类型", ["全部", "高校/科研", "企业"], horizontal=True)
    ii = I.copy()
    if kind == "高校/科研":
        ii = ii[ii["类型"].isin(["education", "facility", "government", "nonprofit", "healthcare"])]
    elif kind == "企业":
        ii = ii[ii["类型"] == "company"]
    ii["国家"] = ii["国家/地区"].map(country_cn)
    top = ii.head(30)
    st.plotly_chart(px.bar(top, x="赛道论文数", y="机构", color="国家", orientation="h",
                           hover_data=["作者数", "总被引"], title="机构 Top 30（按 AI4S 论文数）")
                    .update_layout(yaxis=dict(autorange="reversed"), height=780), width="stretch")
    # 每个机构的高潜人才
    hi = f[f["综合分"] >= 70].groupby("当前机构").size().rename("高潜人才数").reset_index()
    st.markdown("#### 高潜人才聚集的机构（当前筛选）")
    st.dataframe(hi.sort_values("高潜人才数", ascending=False).head(50), width="stretch", hide_index=True)

# ---------------------------------------------------------------- 5. 趋势
if PAGE == "trend":
    by = st.radio("按什么看趋势", ["AI方法", "科学领域", "子方向"], horizontal=True,
                  format_func=lambda x: "蛋白子方向" if x == "子方向" else x)
    ex = P.assign(方向=P[by].fillna("").str.split("|")).explode("方向")
    ex = ex[ex["方向"].fillna("") != ""]
    yr = ex.groupby(["年份", "方向"]).size().reset_index(name="论文数")
    this_year = datetime.now().year
    st.plotly_chart(px.line(yr[yr["年份"] < this_year], x="年份", y="论文数", color="方向", markers=True,
                            title="各方向年度论文数（不含未满一年的当年）").update_layout(height=480),
                    width="stretch")
    rec = ex[ex["年份"] >= this_year - 2].groupby("方向").size()
    prev = ex[(ex["年份"] >= this_year - 4) & (ex["年份"] < this_year - 2)].groupby("方向").size()
    gr = pd.DataFrame({"近2年论文": rec, "前2年论文": prev}).fillna(0).astype(int)
    gr["增长率"] = (((gr["近2年论文"] + 1) / (gr["前2年论文"] + 1) - 1) * 100).round(0)
    cit = ex[ex["年份"] >= this_year - 2].groupby("方向")["被引"].mean().round(1)
    gr["近2年篇均被引"] = cit
    st.markdown("#### 方向热度（增长率高 = 学术升温，可对照融资热度找窗口期）")
    st.dataframe(gr.sort_values("增长率", ascending=False), width="stretch",
                 column_config={"增长率": st.column_config.NumberColumn(format="%.0f%%")})

# ---------------------------------------------------------------- 7. 修改动态
if PAGE == "log":
    st.caption("全组最近 300 条跟进记录修改，最新的在最上面。")
    log = store.crm_history(limit=300)
    if log.empty:
        st.info("还没有人修改过跟进记录")
    else:
        who = st.multiselect("只看这些人的修改", sorted(log["修改人"].unique()))
        st.dataframe(log[log["修改人"].isin(who)] if who else log, width="stretch", hide_index=True)

# ---------------------------------------------------------------- 8. 成员管理（仅管理员）
if IS_ADMIN and AUTH and PAGE == "admin":
    if True:
        st.dataframe(store.list_users(), width="stretch", hide_index=True)
        roles = list(store.ROLES)
        fmt = lambda r: store.ROLES[r]
        a, b = st.columns(2)
        with a, st.form("add_user", clear_on_submit=True):
            st.markdown("**添加成员**")
            nu = st.text_input("用户名（字母或数字，登录用）")
            nn = st.text_input("姓名（显示用，例如中文名）")
            nr = st.selectbox("角色", roles, index=roles.index("editor"), format_func=fmt)
            npw = st.text_input("初始密码（至少 8 位，请当面或私下告诉对方）", type="password")
            if st.form_submit_button("添加"):
                try:
                    store.create_user(nu, nn, nr, npw)
                    st.success(f"已添加 {nu}")
                except ValueError as e:
                    st.error(str(e))
        with b, st.form("edit_user"):
            st.markdown("**修改成员**")
            names = store.list_users()["用户名"].tolist()
            eu = st.selectbox("成员", names)
            er = st.selectbox("角色", ["（不改）"] + roles, format_func=lambda r: r if r == "（不改）" else fmt(r))
            ea = st.selectbox("状态", ["（不改）", "启用", "停用"])
            epw = st.text_input("重设密码（留空则不改）", type="password")
            if st.form_submit_button("保存修改"):
                try:
                    store.update_user(eu, role=None if er == "（不改）" else er,
                                      active=None if ea == "（不改）" else ea == "启用")
                    if epw:
                        store.set_password(eu, epw)
                    st.success("已保存")
                    st.rerun()
                except ValueError as e:
                    st.error(str(e))
        st.caption("角色说明：管理员可管理成员；编辑可修改跟进记录；只读只能查看。停用的账号无法登录，历史记录保留。")
