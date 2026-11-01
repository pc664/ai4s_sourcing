"""论文筛选器 v0（规则版）。

论文需同时命中「AI 方法词」和「蛋白/抗体/生物药领域词」才进入赛道。
P1 会用人工校验样本 + SPECTER2 向量训练一个模型替换/补充这里的规则。
"""
from __future__ import annotations

import re
from functools import lru_cache


def abstract_from_inverted(inv: dict | None) -> str:
    """OpenAlex 的摘要以倒排索引存储，这里还原成正文。"""
    if not inv:
        return ""
    pos = {}
    for word, idxs in inv.items():
        for i in idxs:
            pos[i] = word
    return " ".join(pos[i] for i in sorted(pos))


@lru_cache(maxsize=None)
def _pattern(term: str) -> re.Pattern:
    t = re.escape(term.lower())
    # 短词（tcr、plm、cdr…）要求完整匹配，长词允许后缀（antibod → antibody/antibodies）
    tail = r"\b" if len(term) <= 4 else ""
    return re.compile(rf"\b{t}{tail}")


def hits(text: str, terms: list[str]) -> list[str]:
    return [t for t in terms if _pattern(t).search(text)]


def classify_work(work: dict, cfg: dict) -> dict:
    title = (work.get("title") or "").lower()
    abstract = (work.get("abstract") or abstract_from_inverted(work.get("abstract_inverted_index"))).lower()
    text = f"{title} {abstract}"

    ai = hits(text, cfg["ai_terms"])
    dom = hits(text, cfg["domain_terms"])
    ai_in_title = bool(hits(title, cfg["ai_terms"]))
    dom_in_title = bool(hits(title, cfg["domain_terms"]))

    score = min(len(ai), 4) + min(len(dom), 3) + 2 * ai_in_title + 2 * dom_in_title
    relevant = bool(ai) and bool(dom)
    subtopics = [name for name, kws in cfg["subtopics"].items() if hits(text, kws)]

    return {
        "relevant": relevant,
        "confidence": "高" if relevant and score >= 7 else ("中" if relevant else "低"),
        "rule_score": score,
        "ai_hits": ai,
        "domain_hits": dom,
        "subtopics": subtopics,
        "has_abstract": bool(abstract),
    }
