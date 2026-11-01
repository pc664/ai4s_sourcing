"""学术新星 · 创业潜力榜。

谁算「新星」（资格，config.yaml → rising_star）：
    学术年龄不超过 max_academic_age 年、不是「领军」层级、近期仍在活跃发表 AI4S 论文。

新星分（0–100，只在有资格的人之间比较），四项加权：
    影响力  主导（第一/末位/通讯作者）的代表作在「同年同领域」论文里的被引百分位（对新论文公平），加近期被引
    势头    近期 AI4S 产出量和加速度
    独立性  近期以末位/通讯作者身份发表的论文数（正在成为独立 PI 的信号）和第一作者论文数
    创业信号 产业合作、创始人工厂出身、近期跳槽（尤其去企业）、处于创业高发阶段等

排行榜按主学科分领域排名，见软件「🚀 新星榜」页和 Excel「新星榜」表。
"""
from __future__ import annotations

import math
from datetime import date

import pandas as pd

THIS_YEAR = date.today().year

DEFAULTS = {
    "max_academic_age": 12,
    "require_known_age": True,
    "exclude_tiers": ["领军"],
    "min_recent_papers": 2,
    "active_within_years": 1,
    "weights": {"影响力": 0.40, "势头": 0.15, "独立性": 0.20, "创业信号": 0.25},
    "top_n_per_domain": 30,
}


def _pct(s: pd.Series) -> pd.Series:
    return s.rank(pct=True).fillna(0) * 100


def _col(df: pd.DataFrame, name: str, default=0) -> pd.Series:
    return df[name] if name in df else pd.Series(default, index=df.index)


def score_rising(df: pd.DataFrame, cfg: dict | None, recent_years: int = 2) -> pd.DataFrame:
    """在人才表上加：新星资格、四项分、新星分、新星理由。"""
    c = {**DEFAULTS, **(cfg or {})}
    c["weights"] = {**DEFAULTS["weights"], **(c.get("weights") or {})}
    df = df.copy()

    age = _col(df, "学术年龄", 99).fillna(99)
    age_known = _col(df, "学术起始年", None).notna()
    eligible = ((age <= c["max_academic_age"])
                & ~_col(df, "层级", "").isin(c["exclude_tiers"])
                & (_col(df, "近期论文数") >= c["min_recent_papers"])
                & (_col(df, "最近赛道论文年份") >= THIS_YEAR - c["active_within_years"]))
    if c["require_known_age"]:     # 学术年龄要来自作者履历；只靠首篇 AI4S 论文估算的，资深学者会被误判成新人
        eligible &= age_known
    df["新星资格"] = eligible

    for k in ["影响力分", "势头分", "独立性分", "新星分"]:
        df[k] = float("nan")
    df["新星理由"] = ""
    pool = df[eligible]
    if pool.empty:
        return df

    impact = 0.7 * pool["影响力百分位"].fillna(0) + 0.3 * _pct(pool["近期被引"].map(math.log1p))
    momentum = 0.5 * _pct(pool["近期论文数"]) + 0.5 * _pct((pool["近期论文数"] + 1) / (pool["前期论文数"] + 1))
    independence = 0.7 * _pct(pool["近期末位或通讯数"]) + 0.3 * _pct(pool["近期第一作者数"])
    venture = pool["创业信号分"].fillna(0)
    w = c["weights"]
    total = (w["影响力"] * impact + w["势头"] * momentum + w["独立性"] * independence
             + w["创业信号"] * venture) / sum(w.values())

    df.loc[pool.index, "影响力分"] = impact.round(1)
    df.loc[pool.index, "势头分"] = momentum.round(1)
    df.loc[pool.index, "独立性分"] = independence.round(1)
    df.loc[pool.index, "新星分"] = total.round(1)

    def reasons(r) -> str:
        out = []
        if pd.notna(r.get("近期最佳论文百分位")) and r["近期最佳论文百分位"] >= 90:
            out.append(f"近期论文被引居同年同领域前 {max(1, round(100 - r['近期最佳论文百分位']))}%")
        elif r["影响力百分位"] >= 90:
            out.append(f"代表作被引居同年同领域前 {max(1, round(100 - r['影响力百分位']))}%")
        if r["近期末位或通讯数"] >= 2:
            out.append(f"近{recent_years}年 {int(r['近期末位或通讯数'])} 篇末位/通讯作者，已在独立带队")
        if r["近期论文数"] >= 3 and r["近期论文数"] > r["前期论文数"]:
            out.append(f"产出加速（{int(r['前期论文数'])}→{int(r['近期论文数'])}）")
        if r.get("最近加入年份") and r["最近加入年份"] >= THIS_YEAR - 2:
            kind = "企业" if r.get("最近加入机构类型") == "company" else "新机构"
            out.append(f"{int(r['最近加入年份'])} 年加入{kind}{r.get('最近加入机构') or ''}")
        if r.get("当前机构类型") == "company":
            out.append(f"已在企业：{r['当前机构']}（核实是否为初创）")
        if r["产业合作论文占比"] >= 0.3:
            out.append(f"产业合作占比 {int(r['产业合作论文占比'] * 100)}%")
        if r["出身创始人工厂"]:
            out.append("出身创始人工厂型机构")
        if not r["_age_known"]:
            out.append("学术年龄按首篇 AI4S 论文估算，待核实")
        return "；".join(out)

    tmp = pool.assign(_age_known=age_known[pool.index])
    df.loc[pool.index, "新星理由"] = tmp.apply(reasons, axis=1)
    return df


def domain_ranking(df: pd.DataFrame, domain_col: str = "主学科", top_n: int | None = None) -> pd.DataFrame:
    """有新星资格的人，按领域分组、按新星分排名。"""
    pool = df[df["新星资格"].astype(str).str.lower() == "true"].copy()
    pool = pool.sort_values([domain_col, "新星分"], ascending=[True, False])
    pool["领域排名"] = pool.groupby(domain_col).cumcount() + 1
    if top_n:
        pool = pool[pool["领域排名"] <= top_n]
    return pool
