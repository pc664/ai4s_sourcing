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
SECTOR_TYPES = ["学研双栖", "产业研究者", "学界研究者"]   # 通用 AI 赛道：人人都是 AI 研究者，改按学界/产业划分
DEFAULT_LABELS = {"group": "大类", "top": "一级学科", "domain": "科学领域", "method": "AI方法"}


def labels(tax: dict) -> dict[str, str]:
    """各维度在界面上的显示名（AI4S：一级学科 / 科学领域 / AI方法；通用 AI：一级方向 / 研究方向 / 贡献形态）。"""
    return {**DEFAULT_LABELS, **(tax.get("labels") or {})}


def talent_types(tax: dict) -> list[str]:
    """人才类型的可选值，第一个是「交叉/双栖」型。"""
    return SECTOR_TYPES if (tax.get("talent_type") or {}).get("mode") == "sector" else TALENT_TYPES


def sector_type(inst_type, join_type, ind_share, cfg: dict) -> str:
    """学界 / 产业 / 学研双栖：当前在企业 → 产业研究者；在学界但产业合作多或近期加入企业 → 学研双栖。"""
    if str(inst_type or "") == "company":
        return SECTOR_TYPES[1]
    share = 0 if ind_share is None or pd.isna(ind_share) else ind_share
    if share >= cfg.get("sector_min_share", 0.3) or str(join_type or "") == "company":
        return SECTOR_TYPES[0]
    return SECTOR_TYPES[2]


# ------------------------------------------------------------------ 结构
def domain_parent(tax: dict) -> dict[str, str]:
    """二级学科 → 一级学科。"""
    return {sub: top for top, subs in tax["science_domains"].items() for sub in subs}


def domain_group(tax: dict) -> dict[str, str]:
    """领域（一级）→ 大类。"""
    return {top: g for g, tops in (tax.get("groups") or {}).items() for top in tops}


def sub_group(tax: dict) -> dict[str, str]:
    """细分领域（二级）→ 大类。"""
    dg = domain_group(tax)
    return {sub: dg.get(top, UNCLASSIFIED) for sub, top in domain_parent(tax).items()}


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


def _kw_scores(title: str, text: str, groups: dict[str, list[str]]) -> dict[str, int]:
    return {name: 3 * len(hits(title, kws)) + len(hits(text, kws)) for name, kws in groups.items()}


def seed_subs(tax: dict, seed_domains: list[str] | None) -> list[str]:
    """论文的检索来源 → 对应的细分领域（同名的，或 fetch_alias 里列出的）。"""
    seeds = list(seed_domains or [])
    subs = set(domain_order(tax))
    out = [d for d in seeds if d in subs]
    for sub, src in (tax.get("fetch_alias") or {}).items():
        src = [src] if isinstance(src, str) else src
        if any(d in src for d in seeds):
            out.append(sub)
    return list(dict.fromkeys(out))


def tag_work(title: str, abstract: str, primary_field: str | None, tax: dict,
             venue: str | None = None, seed_domains: list[str] | None = None, strict: bool = False) -> dict:
    """给一篇论文打细分领域标签。
    - 科学学科优先：命中科学学科的论文不再记 ai_only_tops 里的 AI 方向（always_tag 里的除外）；
    - 关键词决定标签；科学学科一个关键词都没命中时，按检索来源归类（AI4S 检索抓到的论文一定属于其学科）；
    - strict：论文只来自 AI 方向的检索，科学学科的门槛提高到 strict_min_score，避免顺带提到的词把它拉进科学学科；
      AI 方向同样要求 strict_min_score，达不到时只有「关键词命中过 1 次、且是检索来源对应的方向」才按来源归类，否则记未归类。"""
    title, abstract = (title or "").lower(), (abstract or "").lower()
    text = f"{title} {abstract}"
    subs = {sub: kws for subs in tax["science_domains"].values() for sub, kws in subs.items()}
    ms = tax.get("min_score", {})
    parent = domain_parent(tax)
    ai_tops = set(tax.get("ai_only_tops") or [])
    always = set(tax.get("always_tag") or [])
    hard = tax.get("strict_min_score", 2)
    sc = _kw_scores(title, text, subs)
    seeds = seed_subs(tax, seed_domains)
    rank = lambda xs: sorted(xs, key=lambda k: -sc[k])

    sci_min = max(ms.get("science", 1), hard) if strict else ms.get("science", 1)
    sci_subs = [k for k in subs if parent[k] not in ai_tops and k not in always]
    sci = rank([k for k in sci_subs if sc[k] >= sci_min])
    if not sci and not strict:            # 科学学科检索抓到、但关键词没命中：归到检索的学科
        sci = [k for k in seeds if k in sci_subs][:1]
    keep = rank([k for k in always if sc[k] >= hard])
    if not sci:
        gen = rank([k for k in subs if parent[k] in ai_tops and sc[k] >= hard])
        if not gen:
            gen = [k for k in seeds if (parent[k] in ai_tops or k in always) and sc[k] >= 1][:1]
        keep = list(dict.fromkeys(keep + gen))
    domains = list(dict.fromkeys(sci + keep))
    methods = [n for n, v in sorted(_kw_scores(title, text, tax["ai_methods"]).items(), key=lambda x: -x[1])
               if v >= ms.get("method", 2)]
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


def current_main_and_secondary(weighted: Counter, raw: Counter, order: list[str],
                               sec_share: float = 0.6, sec_min_papers: int = 3) -> tuple[str, str]:
    """按「当前方向」定主细分领域：论文按发表时间加权（越新权重越大），取权重最高的方向。
    副细分领域只在特别明显时才记：加权后达到主方向的 sec_share，且至少 sec_min_papers 篇。"""
    if not weighted:
        return UNCLASSIFIED, ""
    rank = {k: i for i, k in enumerate(order)}
    items = sorted(weighted.items(), key=lambda kv: (-kv[1], rank.get(kv[0], 999)))
    main = items[0][0]
    sec = ""
    if len(items) > 1:
        k, v = items[1]
        if v >= sec_share * items[0][1] and raw.get(k, 0) >= sec_min_papers:
            sec = k
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
        d = d[d["人才类型"] == talent_types(tax)[0]]
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
