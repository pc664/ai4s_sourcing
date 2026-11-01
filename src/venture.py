"""创业信号雷达：结合近期成果，判断一位科学家出来创业的「成熟度」和「潜力」。

成熟度（时机，他是不是快要出来了）
    职业转折    处在博后末期 / 刚拿教职的年龄段（h 指数很高的资深学者不算）；近两年换了机构，尤其是去了企业
    独立带队    近 3 年第一次以末位 / 通讯作者发表（刚开始带学生），以及近期带队论文的数量
    核心团队    近 3 年反复合作（≥2 次）的合作者人数：3–15 人最像潜在的创始团队，上百人的大组不加分
    产业合作上升 近 3 年和企业合作论文的占比，相比再往前 3 年提高了多少
    系统化产出  近 3 年发布开源工具 / 平台 / 系统类成果的篇数（成果开始向产品形态走）

潜力（价值，他的技术值不值得投）
    成果质量    近期主导论文在「同年同领域」的被引排位，加上整体代表作排位
    离产品距离  近 3 年有实验 / 临床 / 真实场景验证的成果篇数
    赛道窗口期  所在领域近两年论文数的增长（OpenAlex 全量计数）
    技术壁垒    近 3 年自建数据集 / 平台 / 基础模型类成果篇数

两个总分都换算成在同一赛道活跃学者中的百分位（0–100），四象限按 quadrant_cut 划分。
每个分数都附「信号清单」：能追溯到具体论文、年份、机构的证据。
"""
from __future__ import annotations

from datetime import date

import pandas as pd

THIS_YEAR = date.today().year
SEP = " ｜ "
QUADRANTS = ["① 现在就该见", "② 技术好，继续跟踪", "③ 想创业，技术待验证", "④ 暂不关注"]
DEFAULTS = {
    "weights_readiness": {"职业转折": 0.25, "独立带队": 0.25, "核心团队": 0.15, "产业合作上升": 0.15, "系统化产出": 0.20},
    "weights_potential": {"成果质量": 0.35, "离产品距离": 0.25, "赛道窗口期": 0.20, "技术壁垒": 0.20},
    "quadrant_cut": 80,
    "min_recent_papers": 1,
}


def _pct(s: pd.Series) -> pd.Series:
    return s.rank(pct=True).fillna(0) * 100


def _num(df: pd.DataFrame, col: str, default=0.0) -> pd.Series:
    return pd.to_numeric(df[col], errors="coerce").fillna(default) if col in df else pd.Series(default, index=df.index)


def _wsum(parts: dict[str, pd.Series], w: dict[str, float]) -> pd.Series:
    tot = sum(w.get(k, 0) for k in parts) or 1
    return sum(parts[k] * w.get(k, 0) for k in parts) / tot


def _short(t, n=60) -> str:
    t = str(t or "")
    return t if len(t) <= n else t[:n - 1] + "…"


def score_venture(df: pd.DataFrame, cfg: dict | None, growth: dict[str, float] | None = None,
                  prev: pd.DataFrame | None = None) -> pd.DataFrame:
    c = {**DEFAULTS, **(cfg or {})}
    df = df.copy()
    for k in ["成熟度分", "潜力分"] + list(c["weights_readiness"]) + list(c["weights_potential"]):
        df[k] = float("nan")
    df["创业象限"], df["信号清单"], df["关键信号"] = "", "", ""
    pool = df[_num(df, "近三年论文数") >= c["min_recent_papers"]]
    if pool.empty:
        return df

    age = _num(pool, "学术年龄", 99)
    sweet = age.map(lambda a: 100 if 4 <= a <= 10 else 60 if 10 < a <= 14 else 30 if a < 4 else 10)
    # 学术年龄有时被拆开的作者号低估：用 h 指数和层级兜底，已经很资深的人不算「处在创业窗口」
    h = _num(pool, "h指数", 0)
    # 档案不全时 h 指数和年龄都可能偏低，再用产出规模兜底：近 3 年 25 篇以上、或本赛道被引 5000 以上，基本是大组负责人
    senior = ((h >= 40) | (pool.get("层级", pd.Series("", index=pool.index)) == "领军")
              | (_num(pool, "近三年论文数") >= 25) | (_num(pool, "赛道总被引") >= 5000))
    sweet = sweet.where(~senior, 10).where(~((h >= 30) & ~senior), sweet.clip(upper=40))
    moved_year = _num(pool, "最近加入年份", 0)
    moved = moved_year >= THIS_YEAR - 2
    to_company = moved & (pool.get("最近加入机构类型", pd.Series("", index=pool.index)).fillna("") == "company")
    career = (0.5 * sweet + 35 * moved + 15 * to_company).clip(0, 100)

    fy = _num(pool, "首次独立年份", 0)
    newly = (fy >= THIS_YEAR - 3) & (fy > 0) & (h < 30)
    indep = (60 * newly + 30 * ((fy > 0) & ~newly) + 0.4 * _pct(_num(pool, "近期末位或通讯数"))).clip(0, 100)

    # 3–15 位反复合作者最像一个创始团队；上百人的大组更像大实验室负责人，不加分
    core_n = _num(pool, "稳定合作者数")
    team = core_n.map(lambda n: 100 if 3 <= n <= 15 else 70 if 15 < n <= 30 else 40 if n > 30 else 30 * n)

    rs, ps = _num(pool, "近期产业合作占比"), _num(pool, "前期产业合作占比")
    industry = (200 * (rs - ps) + 40 * rs).clip(0, 100)

    systems = (40 * _num(pool, "系统化成果数")).clip(0, 100)

    quality = 0.6 * _num(pool, "近期最佳论文百分位") + 0.4 * _num(pool, "影响力百分位")
    product = (50 * _num(pool, "验证成果数")).clip(0, 100)
    g = pd.Series(growth or {}, dtype=float)
    g_pct = (g.rank(pct=True) * 100) if len(g) > 1 else pd.Series(50.0, index=g.index)
    window = pool["主学科"].map(g_pct).fillna(50.0) if "主学科" in pool else pd.Series(50.0, index=pool.index)
    moat = (40 * _num(pool, "壁垒成果数")).clip(0, 100)

    rparts = {"职业转折": career, "独立带队": indep, "核心团队": team, "产业合作上升": industry, "系统化产出": systems}
    pparts = {"成果质量": quality, "离产品距离": product, "赛道窗口期": window, "技术壁垒": moat}
    for k, v in {**rparts, **pparts}.items():
        df.loc[pool.index, k] = v.round(1)
    ready = _pct(_wsum(rparts, c["weights_readiness"]))
    poten = _pct(_wsum(pparts, c["weights_potential"]))
    df.loc[pool.index, "成熟度分"] = ready.round(1)
    df.loc[pool.index, "潜力分"] = poten.round(1)
    cut = c["quadrant_cut"]
    df.loc[pool.index, "创业象限"] = [
        QUADRANTS[0] if r >= cut and p >= cut else QUADRANTS[1] if p >= cut else QUADRANTS[2] if r >= cut else QUADRANTS[3]
        for r, p in zip(ready, poten)]

    def evidence(r) -> list[str]:
        out = []   # (年份, 文字)，越新越靠前
        if r.get("首次独立年份") and r["首次独立年份"] > 0:
            y = int(r["首次独立年份"])
            out.append((y, f"{y} 首次以末位/通讯作者发表《{_short(r['首次独立论文'])}》"
                           + ("，刚开始独立带队" if y >= THIS_YEAR - 3 else "")))
        if r.get("最近加入年份") and r["最近加入年份"] >= THIS_YEAR - 2:
            kind = "企业" if r.get("最近加入机构类型") == "company" else "新机构"
            out.append((int(r["最近加入年份"]), f"{int(r['最近加入年份'])} 加入{kind}：{r.get('最近加入机构') or '—'}"))
        for col, label in (("系统化", "开源/系统/平台类成果"), ("验证", "有实验或真实场景验证的成果"),
                           ("壁垒", "自建数据/平台/基础模型类成果")):
            n = r.get(f"{col}成果数") or 0
            if n:
                y = int(r[f"{col}年份"]) if pd.notna(r.get(f"{col}年份")) else THIS_YEAR
                out.append((y, f"{y} {label}《{_short(r[f'{col}代表'])}》" + (f"（近 3 年共 {int(n)} 篇）" if n > 1 else "")))
        rs_, ps_ = r.get("近期产业合作占比"), r.get("前期产业合作占比")
        if pd.notna(rs_) and rs_ >= 0.2 and (pd.isna(ps_) or rs_ - ps_ >= 0.1):
            prev_txt = "—" if pd.isna(ps_) else f"{ps_:.0%}"
            out.append((THIS_YEAR, f"近 3 年产业合作占比 {prev_txt} → {rs_:.0%}"))
        if (r.get("稳定合作者数") or 0) >= 3:
            out.append((THIS_YEAR, f"近 3 年有 {int(r['稳定合作者数'])} 位反复合作者（{r['稳定合作者']}）"))
        bp = r.get("近期最佳论文百分位")
        if pd.notna(bp) and bp >= 90:
            out.append((THIS_YEAR, f"近期主导论文被引居同年同领域前 {max(1, round(100 - bp))}%"))
        gr = (growth or {}).get(r.get("主学科"))
        if gr is not None and gr >= 0.3:
            out.append((THIS_YEAR - 1, f"所在领域「{r['主学科']}」近两年论文增长 {gr:.0%}"))
        return [t for _, t in sorted(out, key=lambda x: -x[0])]

    ev = pool.apply(evidence, axis=1)
    df.loc[pool.index, "信号清单"] = ev.map(SEP.join)
    df.loc[pool.index, "关键信号"] = ev.map(lambda x: SEP.join(x[:3]))

    # 与上一次运行对比：新出现的信号、分数变化（每周更新后看「本周新信号」）
    df["新增信号"], df["成熟度变化"], df["潜力变化"] = "", float("nan"), float("nan")
    if prev is not None and len(prev) and "信号清单" in prev:
        p = prev.set_index("author_id")
        old_sig = p["信号清单"].fillna("").map(lambda s: set(s.split(SEP)) if s else set())
        for col, new in (("成熟度变化", "成熟度分"), ("潜力变化", "潜力分")):
            if new in p:
                df[col] = (df[new] - df["author_id"].map(p[new])).round(1)
        def added(row):
            if row["author_id"] not in old_sig.index:
                return ""          # 新进入人才库的人，不算「新增信号」，避免刷屏
            olds = old_sig[row["author_id"]]
            return SEP.join(s for s in (row["信号清单"] or "").split(SEP) if s and s not in olds)
        df["新增信号"] = df.apply(added, axis=1)
    return df
