"""推荐：根据人才收藏里的人的共性，从人才库里找相似的人。

共性从五个方面看（权重见 WEIGHTS）：
    研究主题  收藏的人论文标题里反复出现、而在全库里不常见的词（如 self-evolving、world model）
    合作关系  和几位收藏的人合著过论文
    领域      收藏的人主要集中在哪些领域
    层级      收藏的人主要是哪些层级（领军 / 资深 / 青年骨干 …）
    综合分    在相似的人里优先推荐实力和势头更强的
已收藏的人、以及和已收藏的人同名的记录（多半是同一个人被拆开的档案）不会被推荐。
"""
from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter

import pandas as pd

VERSION = 4   # 改了推荐逻辑或推荐理由的写法就加 1：网页会丢掉旧的缓存结果重新算
WEIGHTS = {"研究主题": 0.35, "合作关系": 0.25, "领域": 0.20, "层级": 0.05, "综合分": 0.15}
STOP = set("""the and for with from that this are was were via using use based into towards toward new novel study
analysis approach method methods model models learning deep neural network networks data large language machine
artificial intelligence its their our can not via between through over under more than how what when where why
towards beyond all one two first efficient effective improved improving framework system systems paper results
high low end task tasks training trained general generalized understanding survey review case application
applications performance evaluation evaluating learn problem problems prediction predicting predict your better
less unified across towards toward open world""".split())


def norm_name(s) -> str:
    s = unicodedata.normalize("NFKD", str(s or ""))
    return re.sub(r"[^a-z]", "", "".join(c for c in s if not unicodedata.combining(c)).lower())


def terms(title: str) -> set[str]:
    """标题里的词和相邻两个词的词组（去掉常见虚词和泛词）。"""
    ws = [w for w in re.findall(r"[a-z][a-z0-9\-]{2,}", str(title or "").lower())]
    keep = [w for w in ws if w not in STOP]
    out = set(keep)
    out.update(f"{a} {b}" for a, b in zip(ws, ws[1:]) if a not in STOP and b not in STOP)
    return out


def recommend(T: pd.DataFrame, A: pd.DataFrame, P: pd.DataFrame, fav_ids: list[str], top_n: int = 100,
              weights: dict | None = None) -> tuple[pd.DataFrame, dict]:
    """返回 (推荐名单, 收藏画像)。推荐名单含 推荐分、各项得分、推荐理由。"""
    w = {**WEIGHTS, **(weights or {})}
    fav = T[T["author_id"].isin(fav_ids)]
    if fav.empty:
        return T.iloc[0:0].assign(推荐分=[], 推荐理由=[]), {}
    fav_set = set(fav["author_id"])
    fav_names = set(fav["姓名"].map(norm_name))
    cand = T[~T["author_id"].isin(fav_set) & ~T["姓名"].map(norm_name).isin(fav_names)].copy()

    # ---- 研究主题：收藏的人标题里的特征词
    aw = A[["work_id", "author_id"]].drop_duplicates()
    wt = dict(zip(P["work_id"], P["标题"].map(terms)))
    n_works = max(1, len(wt))
    df_global = Counter(t for ts in wt.values() for t in ts)
    fav_works = aw[aw["author_id"].isin(fav_set)]
    per_fav = fav_works.groupby("author_id")["work_id"].apply(lambda s: set().union(*[wt.get(x, set()) for x in s]))
    df_fav = Counter(t for ts in per_fav for t in ts)
    n_fav = max(1, len(per_fav))
    # 单个词太泛（全库 1% 以上的论文都有）的不算；词组更能说明方向，加权
    score_t = {t: (c / n_fav) * math.log(n_works / df_global[t]) * (1.5 if " " in t else 1.0)
               for t, c in df_fav.items()
               if c >= max(2, 0.03 * n_fav) and df_global[t] >= 3 and (" " in t or df_global[t] <= 0.01 * n_works)}
    vocab = dict(sorted(score_t.items(), key=lambda kv: -kv[1])[:200])
    work_score = {wid: ts & vocab.keys() for wid, ts in wt.items()}
    work_score = {wid: ts for wid, ts in work_score.items() if ts}
    cw = aw[aw["author_id"].isin(set(cand["author_id"])) & aw["work_id"].isin(work_score.keys())]
    hit = cw.groupby("author_id")["work_id"].apply(lambda s: set().union(*[work_score[x] for x in s]))
    topic_raw = hit.map(lambda ts: sum(vocab[t] for t in ts))
    cand["_topic_terms"] = cand["author_id"].map(hit)
    cand["_topic"] = cand["author_id"].map(topic_raw).fillna(0)
    cand["主题分"] = (cand["_topic"].rank(pct=True) * 100).where(cand["_topic"] > 0, 0).round(0)

    # ---- 合作关系：和几位收藏的人合著过
    # 作者特别多的论文（大型综述、联合署名）不算合作关系
    n_auth = aw.groupby("work_id")["author_id"].size()
    small = set(n_auth[n_auth <= 15].index)
    fw = set(fav_works["work_id"]) & small
    shared = aw[aw["work_id"].isin(fw)]
    pairs = shared[~shared["author_id"].isin(fav_set)].merge(
        fav_works[fav_works["work_id"].isin(fw)].rename(columns={"author_id": "fav_id"}), on="work_id")
    co_n = pairs.groupby("author_id")["fav_id"].nunique()
    name_of = dict(zip(fav["author_id"], fav["姓名"]))
    co_who = pairs.groupby("author_id")["fav_id"].apply(
        lambda s: "、".join(name_of.get(x, x) for x in s.value_counts().index[:3]))
    cand["合作的收藏人数"] = cand["author_id"].map(co_n).fillna(0).astype(int)
    cand["_co_who"] = cand["author_id"].map(co_who)
    cand["合作分"] = (cand["合作的收藏人数"].clip(upper=3) / 3 * 100).round(0)

    # ---- 领域、层级：收藏的人的分布
    dom = fav["主学科"].value_counts(normalize=True)
    tier = fav["层级"].value_counts(normalize=True) if "层级" in fav else pd.Series(dtype=float)
    cand["领域分"] = (cand["主学科"].map(dom).fillna(0) / (dom.max() or 1) * 100).round(0)
    cand["层级分"] = (cand["层级"].map(tier).fillna(0) / (tier.max() or 1) * 100).round(0) if len(tier) else 0

    total = (w["研究主题"] * cand["主题分"] + w["合作关系"] * cand["合作分"] + w["领域"] * cand["领域分"]
             + w["层级"] * cand["层级分"] + w["综合分"] * cand["综合分"].fillna(0)) / sum(w.values())
    cand["推荐分"] = total.round(1)

    def why(r) -> str:
        """推荐理由只写这个人自己的研究主题和领域，不提是和谁比出来的。"""
        out = []
        ts = r["_topic_terms"]
        if isinstance(ts, set) and ts:
            best = sorted(ts, key=lambda t: (" " not in t, -vocab[t]))      # 词组优先
            best = [t for t in best if not any(t != u and t in u.split() for u in best[:6])][:4]
            out.append("、".join(best))
        return "；".join(out)

    cand["推荐理由"] = cand.apply(why, axis=1)
    cand = cand[(cand["推荐理由"] != "") | (cand["合作的收藏人数"] > 0)].sort_values("推荐分", ascending=False)
    cand = cand.assign(_k=cand["姓名"].map(norm_name)).drop_duplicates("_k")   # 同名记录只推荐一条
    profile = {
        "人数": len(fav),
        "领域": [(k, int(round(v * len(fav)))) for k, v in dom.head(6).items()],
        "层级": [(k, int(round(v * len(fav)))) for k, v in tier.head(4).items()],
        "机构": list(fav["当前机构"].dropna().value_counts().head(6).items()),
        "主题词": [t for t in vocab if " " in t][:12] or list(vocab)[:12],
    }
    return cand.drop(columns=["_topic_terms", "_topic", "_co_who", "_k"]).head(top_n), profile
