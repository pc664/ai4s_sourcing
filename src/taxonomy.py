"""人才地图分类体系：科学领域 × AI方法 + 人才类型 / 层级。

论文级：按 config.yaml → taxonomy 的关键词给每篇论文打「科学领域」「AI方法」标签。
作者级：聚合作者所有赛道论文，取出现最多的为主标签、第二多的为副标签；
        再根据主题分布判定人才类型（S / A / A×S），根据学术年龄与 h 指数判定层级。
"""
from __future__ import annotations

from collections import Counter

import pandas as pd

from src.classify import hits

UNCLASSIFIED = "未归类"
TALENT_TYPES = ["A×S 交叉型", "A AI研究者", "S 科学家"]


# ------------------------------------------------------------------ 结构
def domain_parent(tax: dict) -> dict[str, str]:
    """二级学科 → 一级学科。"""
    return {sub: top for top, subs in tax["science_domains"].items() for sub in subs}


def domain_order(tax: dict) -> list[str]:
    return [sub for subs in tax["science_domains"].values() for sub in subs]


def method_order(tax: dict) -> list[str]:
    return list(tax["ai_methods"]) + [tax["ai_method_fallback"]]


# ------------------------------------------------------------------ 论文级
def _scored(title: str, text: str, groups: dict[str, list[str]], min_score: int,
            bonus: dict[str, int] | None = None) -> list[str]:
    out = []
    for name, kws in groups.items():
        s = 3 * len(hits(title, kws)) + len(hits(text, kws)) + (bonus or {}).get(name, 0)
        if s >= min_score:
            out.append((s, name))
    return [n for _, n in sorted(out, key=lambda x: -x[0])]


def tag_work(title: str, abstract: str, primary_field: str | None, tax: dict,
             venue: str | None = None, seed_domains: list[str] | None = None) -> dict:
    """seed_domains：这篇论文是按哪些领域的检索式抓到的，给这些领域各加 1 分。"""
    title, abstract = (title or "").lower(), (abstract or "").lower()
    text = f"{title} {abstract}"
    subs = {sub: kws for subs in tax["science_domains"].values() for sub, kws in subs.items()}
    ms = tax.get("min_score", {})
    domains = _scored(title, text, subs, ms.get("science", 1), {d: 1 for d in seed_domains or []})
    methods = _scored(title, text, tax["ai_methods"], ms.get("method", 2))
    return {
        "科学领域": domains,
        "AI方法": methods or [tax["ai_method_fallback"]],
        "AI侧论文": is_ai_side(primary_field, venue, tax["talent_type"]),
    }


def is_ai_side(primary_field: str | None, venue: str | None, cfg: dict) -> bool:
    """论文主题领域属于 AI 侧，或发表在 AI 平台（arXiv、NeurIPS 等）。"""
    if (primary_field or "") in cfg["ai_fields"]:
        return True
    v = (venue or "").lower()
    return any(k.lower() in v for k in cfg.get("ai_venues", []))


# ------------------------------------------------------------------ 作者级
def main_and_secondary(counter: Counter, order: list[str], share: float) -> tuple[str, str]:
    if not counter:
        return UNCLASSIFIED, ""
    rank = {k: i for i, k in enumerate(order)}
    items = sorted(counter.items(), key=lambda kv: (-kv[1], rank.get(kv[0], 999)))
    main = items[0][0]
    sec = items[1][0] if len(items) > 1 and items[1][1] >= max(1, share * items[0][1]) else ""
    return main, sec


def career_ai_share(profile: dict, ai_fields: list[str]) -> float | None:
    """作者整个学术生涯中 AI 侧主题的占比（需要作者接口返回 topics）。"""
    topics = profile.get("topics") or []
    total = sum(t.get("count") or 0 for t in topics)
    if not total:
        return None
    ai = sum(t.get("count") or 0 for t in topics
             if ((t.get("field") or {}).get("display_name") in ai_fields))
    return round(ai / total, 2)


def talent_type(share: float | None, cfg: dict, career: bool = True) -> str:
    """career=True 表示 share 来自整个学术生涯的主题分布；False 表示只来自赛道论文。"""
    if share is None or pd.isna(share):
        return "S 科学家"
    a_min = cfg["a_min"] if career else cfg.get("track_a_min", cfg["a_min"])
    s_max = cfg["s_max"] if career else cfg.get("track_s_max", cfg["s_max"])
    if share >= a_min:
        return "A AI研究者"
    if share <= s_max:
        return "S 科学家"
    return "A×S 交叉型"


def tier(age, h, rules: list[dict]) -> str:
    age = 0 if age is None or pd.isna(age) else age
    h_known = h is not None and not pd.isna(h)
    for r in rules:
        if age < r.get("min_age", 0):
            continue
        if "min_h" in r and (not h_known or h < r["min_h"]):
            continue
        return r["name"]
    return rules[-1]["name"]


def matrix(df: pd.DataFrame, tax: dict, level: str = "二级", value: str = "人数",
           include_secondary: bool = False) -> pd.DataFrame:
    """AI方法 × 科学领域 计数矩阵。value: 人数 / 高潜人数 / 交叉型人数 / 已接触人数。"""
    parent = domain_parent(tax)
    cols = (list(tax["science_domains"]) if level == "一级" else domain_order(tax)) + [UNCLASSIFIED]
    idx = method_order(tax)
    d = df
    if value == "高潜人数":
        d = d[d["综合分"] >= 70]
    elif value == "交叉型人数":
        d = d[d["人才类型"] == "A×S 交叉型"]
    elif value == "已接触人数":
        d = d[d["关系强度"].astype(str).str.startswith(("L1", "L2"))] if "关系强度" in d else d.iloc[0:0]
    if d.empty:
        return pd.DataFrame(0, index=idx, columns=cols)

    def tags(main: str, sec: str, to_parent: bool) -> list[list[str]]:
        mains = d[main].fillna(UNCLASSIFIED).tolist()
        secs = d[sec].fillna("").tolist() if include_secondary and sec in d else [""] * len(d)
        out = []
        for a, b in zip(mains, secs):
            v = [a] + ([b] if b else [])
            if to_parent:
                v = [parent.get(x, UNCLASSIFIED) for x in v]
            out.append(list(dict.fromkeys(v)))
        return out

    long = pd.DataFrame({"_id": range(len(d)),
                         "AI方法": tags("主方法", "副方法", False),
                         "科学领域": tags("主学科", "副学科", level == "一级")})
    long = long.explode("AI方法").explode("科学领域")
    m = long.groupby(["AI方法", "科学领域"])["_id"].nunique().unstack(fill_value=0)
    return m.reindex(index=idx, columns=cols, fill_value=0).astype(int)
