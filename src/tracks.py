"""赛道：同一套软件跑多个相互独立的人才库（AI4S、通用 AI……）。

config.yaml 顶层是默认赛道（AI4S）的配置；tracks 段里的其他赛道只写「与默认不同的部分」，
运行时叠加到默认配置上：
    - 字典逐层合并，列表和其他值整体替换；
    - taxonomy.science_domains（领域划分）整体替换；
    - classifier.extra_ai_terms 追加到 AI 方法词后面。
每个赛道的结果单独存放（output/app_data_<赛道>/），跟进记录、团队收藏、账号全赛道共享。
"""
from __future__ import annotations

import copy
from pathlib import Path

DEFAULT = "ai4s"


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict) and k != "science_domains":
            out[k] = _merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def names(cfg: dict) -> dict[str, str]:
    """赛道 id → 显示名（默认赛道在最前）。"""
    out = {DEFAULT: cfg.get("track_name", "AI4S")}
    for k, v in (cfg.get("tracks") or {}).items():
        if k != DEFAULT:
            out[k] = (v or {}).get("track_name", k)
    return out


def apply(cfg: dict, track: str | None) -> dict:
    if not track or track == DEFAULT:
        return cfg
    tracks = cfg.get("tracks") or {}
    if track not in tracks:
        raise SystemExit(f"未知赛道：{track}。可选：{list(names(cfg))}")
    over = copy.deepcopy(tracks[track])
    if "fetch" in over and "domains" in over["fetch"]:
        domains = over["fetch"].pop("domains")
    else:
        domains = None
    merged = _merge({k: v for k, v in cfg.items() if k != "tracks"}, over)
    if domains is not None:   # 领域检索式整体替换，不和默认赛道的领域混在一起
        merged["fetch"]["domains"] = domains
    extra = (merged.get("classifier") or {}).pop("extra_ai_terms", None)
    if extra:
        merged["classifier"]["ai_terms"] = list(dict.fromkeys(merged["classifier"]["ai_terms"] + extra))
    return merged


def app_dir(root: Path, track: str | None) -> Path:
    return root / "output" / ("app_data" if not track or track == DEFAULT else f"app_data_{track}")


def query(fc: dict, domain: str) -> str:
    """领域检索式；赛道的 ai_clause 为空时只用领域检索式本身（通用 AI 赛道的领域词本身就是 AI 词）。"""
    clause = fc.get("ai_clause") or ""
    return f"{fc['domains'][domain]} AND {clause}" if clause.strip() else fc["domains"][domain]
