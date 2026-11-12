"""AI 人才地图 · 可视化软件（Streamlit）

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
from src.store import CRM_COLS, OVERRIDABLE

ROOT = Path(__file__).parent
DATA = ROOT / "output" / "app_data"
LEGACY_CRM = ROOT / "crm.csv"    # 旧版跟进记录，首次启动时自动导入数据库 team.db
CFG = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
TAX = CFG.get("taxonomy") or {}
RELATIONS = ["已建联", "未建联"]


def rel_norm(v) -> str:
    """关系：已建联 / 未建联（兼容旧版的 L1 共研 / L2 有接触 / L3 未接触）。"""
    v = str(v or "")
    if v in RELATIONS:
        return v
    return RELATIONS[0] if v.startswith(("L1", "L2")) else RELATIONS[1]
TIERS = list(dict.fromkeys(r["name"] for r in TAX.get("tiers", [])))
DOMAINS = tx.domain_order(TAX) + [tx.UNCLASSIFIED] if TAX else []
METHODS = tx.method_order(TAX) if TAX else []
PARENT = tx.domain_parent(TAX) if TAX else {}
AUTH = (CFG.get("auth") or {}).get("enabled", True)
CN = {"US": "美国", "CN": "中国", "GB": "英国", "DE": "德国", "CH": "瑞士", "FR": "法国",
      "JP": "日本", "KR": "韩国", "CA": "加拿大", "IL": "以色列", "NL": "荷兰", "SG": "新加坡",
      "AU": "澳大利亚", "IN": "印度", "SE": "瑞典", "DK": "丹麦", "ES": "西班牙", "IT": "意大利",
      "BE": "比利时", "AT": "奥地利", "HK": "中国香港", "TW": "中国台湾", "FI": "芬兰", "NO": "挪威"}

st.set_page_config(page_title="AI 人才地图", page_icon="🧬", layout="wide")
ui.inject()   # 深色终端风主题（样式和图表配色）


# ---------------------------------------------------------------- 登录
def login_gate() -> dict:
    if not AUTH:
        return {"username": "local", "name": "本地用户", "role": "admin"}
    if st.session_state.get("user"):
        return st.session_state["user"]
    _, mid, _ = st.columns([1, 1.2, 1])
    with mid:
        st.title("🧬 AI 人才地图")
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
    st.title("🧬 AI 人才地图")
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
LBL = tx.labels(TAX)                     # 维度显示名：大类 / 领域
GROUPS = list(TAX.get("groups") or {})   # 大类：AI研究者 / 科学家
TOPGROUP = tx.domain_group(TAX)          # 领域 → 大类
RENAMED = TAX.get("renamed") or {}       # 旧分类名 → 新分类名
EDITABLE = ["层级", "主学科"]   # 人才画像里可以人工修正的分类
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
    t["关系"] = t["关系强度"].map(rel_norm)
    t = t.drop(columns=[c for c in ["状态", "关系强度", "转化意向", "核实状态"] if c in t])
    if "主学科(人工)" in t:   # 以前人工修正的领域：旧名字换成新名字，已经不存在的分类不再生效
        m = t["主学科(人工)"].fillna("").map(lambda v: RENAMED.get(v, v))
        t["主学科(人工)"] = m.where(m.isin(DOMAINS), "")
    for c in OVERRIDABLE:
        t[f"{c}(自动)"] = t[c]
        manual = t[f"{c}(人工)"].fillna("")
        t[c] = manual.where(manual != "", t[c])
    t["一级学科"] = t["主学科"].map(PARENT).fillna(tx.UNCLASSIFIED)
    t["大类"] = t["一级学科"].map(TOPGROUP).fillna(tx.UNCLASSIFIED)
    for c in ["副学科", "副方法"]:
        t[c] = t[c].fillna("")
    return t


def tag_match(df: pd.DataFrame, main: str, sec: str, picked: list[str], with_sec: bool) -> pd.Series:
    m = df[main].isin(picked)
    return m | df[sec].isin(picked) if with_sec else m


if not (DATA / "talents.csv").exists():
    st.error(f"「{TRACKS[TRACK]}」赛道还没有数据。请先在终端进入软件文件夹运行：`{RUN_CMD}`"
             f"（想先试跑几分钟看效果：`{RUN_CMD} --quick`），跑完后刷新本页。")
    st.stop()

T, P, A, I = load(max((DATA / f).stat().st_mtime for f in
                         ["talents.csv", "papers.csv", "authorships.csv", "institutions.csv"]), str(DATA))
UNC = [tx.UNCLASSIFIED] if "主学科" in T and (T["主学科"] == tx.UNCLASSIFIED).any() else []   # 只有数据里真有未归类的人才显示这一项
DOM_UI = tx.domain_order(TAX) + UNC if TAX else []
if not TAX or "主学科" not in T.columns:
    st.warning(f"数据还是旧版，没有分类信息。请在终端运行：`{RUN_CMD} --skip-fetch`"
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
    """人名旁边的收藏按钮：未收藏时可加入，已收藏时可取消。"""
    row = FAV[FAV["author_id"] == aid]
    if len(row):
        r = row.iloc[0]
        label = "⭐ 已收藏"
        st.caption(f"{r['收藏人']} 于 {r['收藏时间'][:10]} 收藏")
    else:
        label = "☆ 加入团队收藏"
    # 同名的人已经收藏过：多半是同一个人被拆成了多个档案，提醒一下，避免重复收藏
    same = FAV[(FAV["author_id"] != aid) & (FAV["姓名"].map(norm_name) == norm_name(name))] if not len(row) else FAV.iloc[0:0]
    if len(same):
        label = "☆ 加入团队收藏（已有同名收藏）"
    with st.popover(label, disabled=not CAN_EDIT, help=None if CAN_EDIT else "只读账号不能收藏"):
        if len(same):
            st.warning("团队收藏里已经有同名的人：" + "、".join(same["姓名"]) +
                       "。如果是同一个人（档案被拆开了），不用重复收藏；确认是不同的人再加入。")
        if len(row):
            if st.button("取消收藏", key=f"favdel_{key}", width="stretch"):
                store.remove_favorite(aid, USER["name"])
                st.rerun()
        elif st.button("仍然加入" if len(same) else "加入收藏", key=f"favok_{key}", type="primary", width="stretch"):
            store.add_favorite(aid, name, USER["name"])
            st.rerun()



# ---------------------------------------------------------------- 侧边栏筛选
with st.sidebar:
    if AUTH:
        u1, u2 = st.columns([2, 1])
        u1.caption(f"👤 {USER['name']}（{store.ROLES.get(USER['role'], USER['role'])}）")
        if u2.button("退出", width="stretch"):
            st.session_state.pop("user", None)
            st.rerun()
    st.caption(f"{len(T):,} 位人才 · {len(P):,} 篇论文")
    st.divider()
    only_fav = st.toggle(f"⭐ 只看团队收藏（{len(FAV_IDS)}）", key="only_fav")
    st.markdown("**分类筛选**")
    f_grp = st.multiselect(LBL["group"], GROUPS + UNC)
    f_dom = st.multiselect(LBL["domain"], [d for d in DOM_UI
                                           if not f_grp or TOPGROUP.get(PARENT.get(d), tx.UNCLASSIFIED) in f_grp])
    with_sec = st.checkbox("同时匹配副领域", help="不勾选时只按主领域筛选")
    f_tier = st.multiselect("层级", TIERS)
    f_rel = st.multiselect("关系", RELATIONS)
    with st.expander("更多筛选"):
        countries = st.multiselect("国家/地区", sorted(T["国家"].unique()))
        stages = st.multiselect("职业阶段", [s for s in ["博士生/博后", "博后末期/青年PI", "PI中期", "资深"]
                                         if s in set(T["职业阶段"])])
        min_score = st.slider("综合分不低于", 0, 100, 0)
    kw = st.text_input("搜索姓名 / 机构", help="支持中文名（如 周煊赫）、不同拼写（Xuanhe Zhou / Xuan-He Zhou / Zhou Xuanhe）")

f = T.copy()
if only_fav:
    f = f[f["⭐"] == "⭐"]
if f_grp:
    f = f[f["大类"].isin(f_grp)]
if f_dom:
    f = f[tag_match(f, "主学科", "副学科", f_dom, with_sec)]
for col, picked in [("层级", f_tier), ("关系", f_rel)]:
    if picked:
        f = f[f[col].isin(picked)]
if countries:
    f = f[f["国家"].isin(countries)]
if stages:
    f = f[f["职业阶段"].isin(stages)]
f = f[f["综合分"] >= min_score]
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
ui.top_bar("AI TALENT TERMINAL",
           "Scientist sourcing · rising stars · early-stage",
           [("TALENTS", f"{len(T):,}", ""),
            ("RISING STARS", f"{int((T['新星资格'].astype(str).str.lower() == 'true').sum()) if '新星资格' in T else 0:,}", "r"),
            ("PAPERS", f"{len(P):,}", "a"), ("WATCHLIST", f"{len(FAV_IDS)}", "g")],
           ui.now_text((DATA / "talents.csv").stat().st_mtime))

PAGES = {"star": "🚀 新星榜", "fav": f"⭐ 团队收藏（{len(FAV_IDS)}）",
         "rank": "🏆 人才线索榜", "person": "👤 人才画像", "inst": "🏛️ 机构", "log": "📝 修改动态"}
if IS_ADMIN and AUTH:
    PAGES["admin"] = "👥 成员管理"
if st.session_state.get("goto_page") in PAGES:       # 程序要求跳页（必须在导航条画出来之前设置）
    st.session_state["page"] = st.session_state.pop("goto_page")
if st.session_state.get("page") not in PAGES:
    st.session_state["page"] = "star"
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

LIST_COLS = ["综合分", "姓名", "大类", "层级", "关系", "当前机构", "国家",
             "主学科", "副学科", "推荐理由"]
SCORE_CFG = {"综合分": st.column_config.ProgressColumn("综合分", min_value=0, max_value=100, format="%.0f"),
             "推荐理由": st.column_config.TextColumn(width="large")}

# ---------------------------------------------------------------- 新星榜
STAR_CFG = {
    "新星分": st.column_config.ProgressColumn("新星分", min_value=0, max_value=100, format="%.0f"),
    "影响力分": st.column_config.ProgressColumn("影响力", min_value=0, max_value=100, format="%.0f"),
    "势头分": st.column_config.ProgressColumn("势头", min_value=0, max_value=100, format="%.0f"),
    "独立性分": st.column_config.ProgressColumn("独立性", min_value=0, max_value=100, format="%.0f"),
    "学术年龄": st.column_config.NumberColumn(format="%d 年"),
    "新星理由": st.column_config.TextColumn(width="large"),
}
STAR_COLS = ["领域排名", "新星分", "⭐", "姓名", "大类", "主学科", "当前机构", "国家", "学术年龄", "层级",
             "影响力分", "势头分", "独立性分", "新星理由", "近期最佳论文", "关系"]

# ---------------------------------------------------------------- 新星榜
if PAGE == "star":
    if not HAS_STAR:
        st.warning(f"数据还没有新星评分。请在终端运行 `{RUN_CMD} --skip-fetch`（几秒到一分钟，不耗 API 额度），然后刷新本页。")
    else:
        stars = f[f["新星资格"].astype(str).str.lower() == "true"].copy()
        c1, c2, c3, c4 = st.columns([1, 2.2, 1.2, 1.2])
        if st.session_state.get("star_level") not in ("二级", "大类"):
            st.session_state["star_level"] = "二级"
        s_level = c1.radio("排名粒度", ["二级", "大类"], horizontal=True, key="star_level",
                           format_func=lambda x: {"二级": LBL["domain"], "大类": LBL["group"]}[x])
        dcol = {"二级": "主学科", "大类": "大类"}[s_level]
        order = {"二级": DOM_UI, "大类": GROUPS + UNC}[s_level]
        counts = stars[dcol].value_counts()
        dom_opts = ["全部领域"] + [d for d in order if counts.get(d, 0)]
        s_dom = c2.selectbox("领域", dom_opts, key="star_dom",
                             format_func=lambda d: f"全部领域（{len(stars)} 人，各领域分别排名）" if d == "全部领域"
                             else f"{d}（{counts.get(d, 0)} 人）")
        s_new = c3.checkbox("只看未建联的", key="star_new")
        s_sec = c4.checkbox("计入副领域", help="副领域是这个领域的新星也一起排名", key="star_sec")
        if s_new:
            stars = stars[stars["关系"] == "未建联"]

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
                sec = stars["副学科"] if s_level == "二级" else stars["副学科"].map(tx.sub_group(TAX))
                in_dom |= sec == s_dom
            sview = stars[in_dom].sort_values("新星分", ascending=False)
            sview["领域排名"] = range(1, len(sview) + 1)
            top_n = st.slider("显示前几名", 10, max(10, min(200, len(sview))), min(30, max(10, len(sview))),
                              key="star_topn") if len(sview) > 10 else len(sview)
            sview = sview.head(top_n)

        st.markdown(f"#### {'各领域新星排行' if s_dom == '全部领域' else s_dom + ' · 新星排行'}（{len(sview)} 人）")
        show = [c for c in STAR_COLS if c in sview.columns]
        hit = ui.dbl_table(sview, [ui.col("领域排名", "排名", "num"), ui.col("新星分", type="bar"), ui.col("⭐"), ui.col("姓名"),
                                   ui.col("大类"), ui.col("主学科", LBL["domain"]), ui.col("当前机构"), ui.col("国家"),
                                   ui.col("学术年龄", type="num", suffix=" 年"), ui.col("层级"),
                                   ui.col("影响力分", "影响力", "bar"), ui.col("势头分", "势头", "bar"),
                                   ui.col("独立性分", "独立性", "bar"),
                                   ui.col("新星理由", wide=True), ui.col("近期最佳论文", wide=True), ui.col("关系")], key=f"star_table_{s_dom}_{s_level}", height=min(700, 40 + 33 * len(sview)))
        if hit:
            open_profile(hit)
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
                            f" · {r['层级']} · {r['大类']}")
                st.markdown(f"`{r['主学科']}` `{r['关系']}`")
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
                k1, k2, k3, k4 = st.columns(4)
                k1.metric("新星分", f"{r['新星分']:.0f}")
                k2.metric("影响力", f"{r['影响力分']:.0f}")
                k3.metric("势头", f"{r['势头分']:.0f}")
                k4.metric("独立性", f"{r['独立性分']:.0f}")
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
        g1, _g2 = st.columns(2)
        fu = g1.multiselect("收藏人", sorted(set(FAV["收藏人"])), key="fav_users")
        cols = ["author_id", "姓名", "当前机构", "国家", "大类", "主学科", "层级", "学术年龄", "综合分",
                "新星分", "关系", "负责人"]
        fv = FAV.merge(T[[c for c in cols if c in T.columns and c != "姓名"]], on="author_id", how="left")
        fv["姓名"] = fv["author_id"].map(dict(zip(T["author_id"], T["姓名"]))).fillna(fv["姓名"])   # 用人才库里最新的写法
        if fu:
            fv = fv[fv["收藏人"].isin(fu)]
        show = [c for c in ["姓名", "当前机构", "国家", "大类", "主学科", "层级", "学术年龄", "综合分", "新星分",
                            "关系", "负责人", "收藏人", "收藏时间"] if c in fv.columns]
        fv["所在赛道"] = ["是" if find_track(a) else "找不到" for a in fv["author_id"]]
        st.markdown(f"#### {len(fv)} 位")
        hit = ui.dbl_table(fv, [ui.col("姓名"), ui.col("当前机构"), ui.col("大类"), ui.col("主学科", LBL["domain"]),
                                ui.col("新星分", type="num"), ui.col("关系"), ui.col("负责人"), ui.col("收藏人"),
                                ui.col("所在赛道", "在库")], key="fav_table")
        if hit:
            open_profile(hit)
        gone = (fv["所在赛道"] == "找不到").sum()
        if gone:
            st.caption(f"有 {gone} 位在最新数据里找不到（可能是数据更新后作者号变了，或论文数不够进人才库），点名字打不开。")
        st.download_button("下载收藏名单（CSV）", fv[show + ["author_id"]].to_csv(index=False).encode("utf-8-sig"),
                           "团队收藏.csv", "text/csv")

# ---------------------------------------------------------------- 2. 线索榜
if PAGE == "rank":
    st.caption(f"共 {len(f):,} 人 · 点击表头排序")
    show = ["综合分", "⭐", "姓名", "大类", "层级", "主学科", "关系",
            "推荐理由", "当前机构", "国家", "职业阶段", "副学科",
            "学术实力分", "增长势头分", "赛道论文数", "近期论文数", "赛道总被引",
            "h指数", "最近加入机构", "最近加入年份", "代表作", "ORCID"]
    show = [c for c in show if c in f.columns]
    view = f[show].copy()
    if len(f) > 1000:
        st.caption("只显示综合分前 1000 位，完整名单请下载")
    spec = {"综合分": ui.col("综合分", type="bar"), "学术实力分": ui.col("学术实力分", "实力", "bar"),
            "增长势头分": ui.col("增长势头分", "势头", "bar"),
            "主学科": ui.col("主学科", LBL["domain"]), "副学科": ui.col("副学科", "副" + LBL["domain"]),
            "推荐理由": ui.col("推荐理由", wide=True), "代表作": ui.col("代表作", wide=True),
            "ORCID": ui.col("ORCID", type="link", text="ORCID"),
            "赛道论文数": ui.col("赛道论文数", "收录论文", "num"), "近期论文数": ui.col("近期论文数", type="num"),
            "赛道总被引": ui.col("赛道总被引", "收录论文被引", "num"), "h指数": ui.col("h指数", type="num"),
            "最近加入年份": ui.col("最近加入年份", type="num", digits=0)}
    hit = ui.dbl_table(f.head(1000), [spec.get(c) or ui.col(c) for c in show], key="rank_table", height=650)
    if hit:
        open_profile(hit)
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
            tags = [p["大类"], f"{p['一级学科']} / {p['主学科']}" if p["一级学科"] != p["主学科"] else p["主学科"],
                    p["层级"], p["关系"]]
            st.markdown(" ".join(f"`{t}`" for t in tags if txt(t, "")))
            if p["副学科"]:
                st.caption(f"副{LBL['domain']}：" + p["副学科"])
            if isinstance(p.get("推荐理由"), str) and p["推荐理由"]:
                st.success("推荐理由：" + p["推荐理由"])
            links = [profile_link(aid)]
            if isinstance(p.get("ORCID"), str):
                links.append(f"[ORCID]({p['ORCID']})")
            links.append(f"[Google Scholar 搜索](https://scholar.google.com/scholar?q=%22{p['姓名'].replace(' ', '+')}%22)")
            st.markdown(" · ".join(links))
        with h2:
            m1, m2, m3 = st.columns(3)
            m1.metric("综合", f"{p['综合分']:.0f}")
            m2.metric("实力", f"{p['学术实力分']:.0f}")
            m3.metric("势头", f"{p['增长势头分']:.0f}")
            n1, n2, n3 = st.columns(3)
            tw, tc, hv = p.get("总发文数"), p.get("总被引"), p.get("h指数")
            n1.metric("总发文", "—" if pd.isna(tw) else f"{int(tw):,}", help="整个学术生涯（来自 Semantic Scholar / OpenAlex 作者档案）")
            n2.metric("总被引", "—" if pd.isna(tc) else f"{int(tc):,}", help="整个学术生涯（来自 Semantic Scholar / OpenAlex 作者档案）")
            n3.metric("h 指数", "—" if pd.isna(hv) else int(hv))
            st.caption(f"其中 {int(p['赛道论文数'])} 篇（被引 {int(p['赛道总被引']):,}）在人才库的检索范围内，"
                       "用于分类和评分")
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
                st.info(f"{name.iloc[0] if len(name) else target} 的论文不足 2 篇，不在人才库里，"
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

            with st.expander(f"合作者列表（{n} 人）"):
                tbl = co[["author_id", "姓名", "机构", "合作次数", "综合分", "在人才库"]].copy()
                tbl["在人才库"] = tbl["在人才库"].map({True: "是", False: "否"})
                tbl["外部主页"] = [("https://www.semanticscholar.org/author/" + a[3:]) if str(a).startswith("S2-")
                                else f"https://openalex.org/{a}" for a in co["author_id"]]
                hit = ui.dbl_table(tbl, [ui.col("姓名"), ui.col("机构"), ui.col("合作次数", type="num"),
                                         ui.col("综合分", type="num"), ui.col("在人才库"),
                                         ui.col("外部主页", type="link", text="打开")], key=f"co_{aid}",
                                   height=min(420, 40 + 33 * len(tbl)))
                if hit:
                    open_person(hit)

        st.markdown(f"#### 人才库收录的论文（{len(works)} 篇）")
        st.caption("人才库只收录每个方向检索范围内的论文（高被引 + 近期 + 最新），不是他的全部论文；完整列表见上面的外部主页链接。")
        st.dataframe(works.sort_values("被引", ascending=False)[["年份", "标题", "被引", "期刊/平台", "DOI"]],
                     width="stretch", hide_index=True,
                     column_config={"DOI": st.column_config.LinkColumn("DOI"),
                                    "年份": st.column_config.NumberColumn(format="%d")})

        with st.expander("自动分类的依据"):
            st.write(f"**{LBL['domain']}按当前方向定**：越新的论文权重越大（每 1.5 年减半），取权重最高的方向；"
                     "副领域只在特别明显时才写。")
            st.write(f"**人才库收录论文的分布**（篇数）：{txt(p.get('学科分布'))}")
            if txt(p.get("近期论文方向"), ""):
                st.write(f"**最近两年全部论文的方向**（加权）：{p['近期论文方向']}")
            st.write(f"**{LBL['group']}**：由{LBL['domain']}「{p['主学科']}」决定 → {p['大类']}")
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
            s1, _s2, _s3, _s4 = st.columns(4)
            rel = pick_opt("关系", RELATIONS, rel_norm(o["关系强度"]), RELATIONS[-1], s1)
            st.caption("自动分类不准时在这里修正；选「自动：…」表示沿用系统判定。")
            choices = {"层级": TIERS, "主学科": tx.domain_order(TAX)}
            ov = {}
            for col, c in zip(st.columns(4), EDITABLE):
                auto = f"自动：{p[f'{c}(自动)']}"
                opts = [auto] + choices[c]
                cur = o.get(f"{c}(人工)", "")
                cur = RENAMED.get(cur, cur) if c == "主学科" else cur
                v = col.selectbox({"主学科": LBL["domain"]}.get(c, c), opts, index=opts.index(cur) if cur in opts else 0)
                ov[f"{c}(人工)"] = "" if v == auto else v
            owner = st.text_input("负责人", o["负责人"] or (USER["name"] if CAN_EDIT else ""))
            note = st.text_area("备注", o["备注"], placeholder="例如：已通过 XX 引荐，下周约电话；关注其新预印本…")
            submitted = st.form_submit_button("保存", disabled=not CAN_EDIT, type="primary")
            if submitted:
                try:
                    n = store.save_crm({"author_id": aid, "姓名": p["姓名"], "状态": o.get("状态", ""), "关系强度": rel,
                                        "转化意向": o.get("转化意向", ""), "核实状态": o.get("核实状态", ""),
                                        "人才类型(人工)": o.get("人才类型(人工)", ""),
                                        "主方法(人工)": o.get("主方法(人工)", ""), **ov, "负责人": owner, "备注": note},
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
                           hover_data=["作者数", "总被引"], title="机构 Top 30（按论文数）")
                    .update_layout(yaxis=dict(autorange="reversed"), height=780), width="stretch")
    # 每个机构的高潜人才
    hi = f[f["综合分"] >= 70].groupby("当前机构").size().rename("高潜人才数").reset_index()
    st.markdown("#### 高潜人才聚集的机构（当前筛选）")
    st.dataframe(hi.sort_values("高潜人才数", ascending=False).head(50), width="stretch", hide_index=True)

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
