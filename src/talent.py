"""人才库构建：把赛道论文聚合到作者，计算画像特征与 sourcing 评分。

评分为启发式版本（可解释、无需标签）：综合分 = 学术实力 50% + 增长势头 50%。
"""
from __future__ import annotations

import json
import math
import re
from collections import Counter, defaultdict
from datetime import date

import pandas as pd

from src import taxonomy as tx

THIS_YEAR = date.today().year


def norm_title(t: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (t or "").lower())[:120]


def apply_recent_papers(df: pd.DataFrame, recent: dict[str, list], tax: dict) -> pd.DataFrame:
    """用作者本人的近期论文（S2 作者主页上的全部论文标题，不限于人才库的检索范围）修正「当前方向」。
    recent：人员号 → [[年份, 标题], …]。只按标题打标签；人才库里已有的论文不重复计算。"""
    if not len(df) or "_dw" not in df:
        return df
    half = tax.get("domain_half_life", 1.5)
    order, parent, group = tx.domain_order(tax), tx.domain_parent(tax), tx.sub_group(tax)
    this_year = THIS_YEAR
    df = df.copy()
    for i, r in df.iterrows():
        items = recent.get(r["author_id"])
        if not items:
            continue
        have = set(str(r["_tn"]).split("|"))
        dw, dr = Counter(json.loads(r["_dw"])), Counter(json.loads(r["_dr"]))
        added = 0
        for year, title in items:
            if not title or norm_title(title) in have:
                continue
            tags = tx.tag_work(title, "", None, tax, strict=True)["科学领域"]
            if not tags:
                continue
            wgt = 0.5 ** (max(0, this_year - (year or 0)) / half)
            for d in tags:
                dw[d] += wgt
                dr[d] += 1
            added += 1
        if not added:
            continue
        main, sec = tx.current_main_and_secondary(dw, dr, order, tax.get("domain_secondary_share", 0.6),
                                                  tax.get("domain_secondary_min_papers", 3))
        df.at[i, "主学科"], df.at[i, "副学科"] = main, sec
        df.at[i, "一级学科"] = parent.get(main, tx.UNCLASSIFIED)
        df.at[i, "大类"] = group.get(main, tx.UNCLASSIFIED)
        df.at[i, "近期论文方向"] = " | ".join(f"{k} {v:.1f}" for k, v in dw.most_common(4))
    return df


def _short(oa_id: str | None) -> str:
    return (oa_id or "").rsplit("/", 1)[-1]


def _best(items: list[tuple]) -> tuple:
    """优先本人主导的，再取最新的一篇。"""
    return max(items, key=lambda x: (x[2], x[0])) if items else (None, "", False)


def _venture_features(r: dict) -> dict:
    pi = sorted(r["pi_papers"])
    core = [(n, c) for n, c in r["v_coauth"].most_common() if c >= 2]
    ri, rn = r["v_ind"]
    pi_, pn = r["v_prev_ind"]
    sys_, val, moat = _best(r["v_sys"]), _best(r["v_val"]), _best(r["v_moat"])
    return {
        "近三年论文数": rn,
        "首次独立年份": pi[0][0] if pi else None,
        "首次独立论文": pi[0][1] if pi else "",
        "稳定合作者数": len(core),
        "稳定合作者": "、".join(n for n, _ in core[:5]),
        "近期产业合作占比": round(ri / rn, 2) if rn else None,
        "前期产业合作占比": round(pi_ / pn, 2) if pn else None,
        "系统化成果数": len(r["v_sys"]), "系统化代表": sys_[1], "系统化年份": sys_[0],
        "验证成果数": len(r["v_val"]), "验证代表": val[1], "验证年份": val[0],
        "壁垒成果数": len(r["v_moat"]), "壁垒代表": moat[1], "壁垒年份": moat[0],
    }


def _top3(xs: list[float]) -> float:
    """最好 3 篇的加权平均（0.5 / 0.3 / 0.2）：更看重最好的代表作，不让一篇弱论文把两篇顶级论文拉下来。"""
    top = sorted(xs, reverse=True)[:3]
    w = [0.5, 0.3, 0.2][:len(top)]
    return round(sum(x * k for x, k in zip(top, w)) / sum(w), 1) if top else 0.0


def build_author_table(works: list[dict], labels: dict[str, dict], tcfg: dict,
                       tax: dict | None = None) -> pd.DataFrame:
    """works: 已判定为赛道相关的论文；labels: work_id -> 分类结果（含科学领域 / AI方法标签）。"""
    recent_from = THIS_YEAR - tcfg["recent_years"] + 1
    prev_from = recent_from - tcfg["recent_years"]
    half_life = (tax or {}).get("domain_half_life", 1.5)   # 定「当前方向」时论文权重的半衰期（年）
    A = defaultdict(lambda: {
        "papers": [], "first": 0, "last": 0, "corr": 0, "cites": 0,
        "years": [], "subtopics": Counter(), "coauthors": Counter(),
        "inst_seq": [], "industry_papers": 0, "name": "", "orcid": None,
        "domains": Counter(), "domains_w": Counter(), "methods": Counter(), "ai_side": 0,
        "r_first": 0, "r_lead": 0, "r_cites": 0, "pcts": [], "lead_pcts": [], "r_best": (-1, ""),
        # 创业信号用：独立带队的时间线、近期合作圈、近期 / 前期产业合作、系统化 / 验证 / 壁垒类成果
        "pi_papers": [], "v_coauth": Counter(), "v_ind": [0, 0], "v_prev_ind": [0, 0],
        "v_sys": [], "v_val": [], "v_moat": [],
    })
    v_from = THIS_YEAR - 2            # 创业信号的「近期」：近 3 年

    for w in works:
        wid = _short(w["id"])
        year = w.get("publication_year") or 0
        auths = w.get("authorships") or []
        has_company = any(
            (i.get("type") == "company") for a in auths for i in (a.get("institutions") or []))
        names = [(a.get("author") or {}).get("display_name") for a in auths]
        for a in auths:
            au = a.get("author") or {}
            aid = _short(au.get("id"))
            if not aid:
                continue
            r = A[aid]
            r["name"] = au.get("display_name") or r["name"]
            r["orcid"] = au.get("orcid") or r["orcid"]
            r["papers"].append((wid, year, w.get("cited_by_count") or 0, w.get("title") or ""))
            r["years"].append(year)
            r["cites"] += w.get("cited_by_count") or 0
            pos = a.get("author_position")
            r["first"] += pos == "first"
            r["last"] += pos == "last" and len(auths) > 1
            r["corr"] += bool(a.get("is_corresponding"))
            r["industry_papers"] += has_company
            r["subtopics"].update(labels[wid]["subtopics"])
            r["domains"].update(labels[wid].get("科学领域", []))
            w_recent = 0.5 ** (max(0, THIS_YEAR - year) / half_life) if year else 0.05   # 越新的论文权重越大
            for d in labels[wid].get("科学领域", []):
                r["domains_w"][d] += w_recent
            r["methods"].update(labels[wid].get("AI方法", []))
            r["ai_side"] += bool(labels[wid].get("AI侧论文"))
            pct = labels[wid].get("被引百分位")
            lead = pos == "first" or (pos == "last" and len(auths) > 1) or bool(a.get("is_corresponding"))
            if pct is not None:
                r["pcts"].append(pct)
                if lead:
                    r["lead_pcts"].append(pct)
            if year >= recent_from:      # 近期（默认近 2 年）
                r["r_first"] += pos == "first"
                r["r_lead"] += (pos == "last" and len(auths) > 1) or bool(a.get("is_corresponding"))
                r["r_cites"] += w.get("cited_by_count") or 0
                if lead and pct is not None and pct > r["r_best"][0]:
                    r["r_best"] = (pct, w.get("title") or "")
            r["coauthors"].update(n for n in names if n and n != r["name"])
            pi = (pos == "last" and len(auths) > 1) or bool(a.get("is_corresponding"))
            if pi:
                r["pi_papers"].append((year, w.get("title") or ""))
            vt = labels[wid].get("创业标签") or {}
            if year >= v_from:
                r["v_coauth"].update(n for n in names if n and n != r["name"])
                r["v_ind"][0] += has_company
                r["v_ind"][1] += 1
                item = (year, w.get("title") or "", lead)
                for k, bucket in (("系统", "v_sys"), ("验证", "v_val"), ("壁垒", "v_moat")):
                    if vt.get(k):
                        r[bucket].append(item)
            elif year >= v_from - 3:
                r["v_prev_ind"][0] += has_company
                r["v_prev_ind"][1] += 1
            for inst in a.get("institutions") or []:
                r["inst_seq"].append((w.get("publication_date") or str(year), inst))

    rows = []
    for aid, r in A.items():
        n = len(r["papers"])
        if n < tcfg["min_track_papers"]:
            continue
        r["inst_seq"].sort(key=lambda x: x[0])
        latest = r["inst_seq"][-1][1] if r["inst_seq"] else {}
        insts_ordered = list(dict.fromkeys(i["display_name"] for _, i in r["inst_seq"]))
        recent = sum(y >= recent_from for y in r["years"])
        prev = sum(prev_from <= y < recent_from for y in r["years"])
        top = max(r["papers"], key=lambda p: p[2])
        tags = {}
        if tax:
            share = tax.get("secondary_min_share", 0.3)
            d_main, d_sec = tx.current_main_and_secondary(r["domains_w"], r["domains"], tx.domain_order(tax),
                                                          tax.get("domain_secondary_share", 0.6),
                                                          tax.get("domain_secondary_min_papers", 3))
            mc = r["methods"]
            prefer = tax.get("method_prefer_specific")
            if prefer:   # 兜底类别（如「算法与方法」）太泛：具体形态的论文够多时，主/副标签只在具体形态里选
                fb = tax["ai_method_fallback"]
                spec = Counter({k: v for k, v in mc.items() if k != fb})
                if sum(spec.values()) >= max(2, prefer * n):
                    mc = spec
            m_main, m_sec = tx.main_and_secondary(mc, tx.method_order(tax), share)
            tags = {
                "大类": tx.sub_group(tax).get(d_main, tx.UNCLASSIFIED),
                "一级学科": tx.domain_parent(tax).get(d_main, tx.UNCLASSIFIED),
                "主学科": d_main, "副学科": d_sec, "主方法": m_main, "副方法": m_sec,
                "学科分布": " | ".join(f"{k} {v}" for k, v in r["domains"].most_common()),
                "_dw": json.dumps(dict(r["domains_w"]), ensure_ascii=False),
                "_dr": json.dumps(dict(r["domains"]), ensure_ascii=False),
                "_tn": "|".join(sorted({norm_title(p[3]) for p in r["papers"]})),
                "方法分布": " | ".join(f"{k} {v}" for k, v in r["methods"].most_common()),
                "赛道AI侧论文占比": round(r["ai_side"] / n, 2),
            }
        rows.append({
            "author_id": aid,
            "姓名": r["name"],
            "ORCID": r["orcid"],
            "赛道论文数": n,
            "第一作者数": r["first"],
            "末位作者数": r["last"],
            "通讯作者数": r["corr"],
            "赛道总被引": r["cites"],
            "首篇赛道论文年份": min(r["years"]),
            "最近赛道论文年份": max(r["years"]),
            "近期论文数": recent,
            "前期论文数": prev,
            "产业合作论文占比": round(r["industry_papers"] / n, 2),
            "主要方向": "、".join(t for t, _ in r["subtopics"].most_common(3)),
            "当前机构": latest.get("display_name"),
            "当前机构类型": latest.get("type"),
            "国家/地区": latest.get("country_code"),
            "赛道内机构轨迹": " → ".join(insts_ordered[-4:]),
            "常合作者": "、".join(n for n, _ in r["coauthors"].most_common(5)),
            "代表作": top[3],
            "代表作被引": top[2],
            "近期第一作者数": r["r_first"],
            "近期末位或通讯数": r["r_lead"],
            "近期被引": r["r_cites"],
            # 主导（第一/末位/通讯作者）的最好 3 篇论文在「同年同领域」里的被引百分位加权平均：
            # 衡量质量、对新论文公平，且不会因为挂名在大团队论文的中间位置而虚高；
            # 没有主导论文时，用全部论文的值打五折
            "影响力百分位": _top3(r["lead_pcts"]) if r["lead_pcts"] else round(0.5 * _top3(r["pcts"]), 1),
            "主导论文数": len(r["lead_pcts"]),
            "近期最佳论文": r["r_best"][1],
            "近期最佳论文百分位": r["r_best"][0] if r["r_best"][0] >= 0 else None,
            **tags,
        })
    return pd.DataFrame(rows)


def enrich_with_author_profiles(df: pd.DataFrame, profiles: list[dict],
                                tax: dict | None = None) -> pd.DataFrame:
    """用作者接口补全：总发文、h 指数、学术年龄、完整机构履历、最近一次跳槽。"""
    P = {}
    for p in profiles:
        affs = p.get("affiliations") or []
        all_years = [y for a in affs for y in (a.get("years") or [])]
        first_year = min(all_years) if all_years else None
        # 每个机构的起始年 → 找最近一次"加入新机构"
        starts = sorted(
            ((min(a.get("years") or [9999]), a["institution"]) for a in affs if a.get("institution")),
            key=lambda x: x[0])
        last_move = starts[-1] if len(starts) > 1 else (None, None)
        P[_short(p["id"])] = {
            "总发文数": p.get("works_count"),
            "总被引": p.get("cited_by_count"),
            "h指数": (p.get("summary_stats") or {}).get("h_index"),
            "学术起始年": first_year,
            "最近加入机构": (last_move[1] or {}).get("display_name"),
            "最近加入机构类型": (last_move[1] or {}).get("type"),
            "最近加入年份": last_move[0],
            "完整机构履历": " → ".join(
                dict.fromkeys(i.get("display_name") for _, i in starts if i)),
            "生涯AI侧占比": tx.career_ai_share(p, tax["talent_type"]["ai_fields"]) if tax else None,
        }
    extra = pd.DataFrame.from_dict(P, orient="index")
    extra.index.name = "author_id"
    return df.merge(extra, left_on="author_id", right_index=True, how="left")


def _pct(s: pd.Series) -> pd.Series:
    return s.rank(pct=True).fillna(0) * 100


def score(df: pd.DataFrame, tcfg: dict, tax: dict | None = None) -> pd.DataFrame:
    df = df.copy()
    start = df.get("学术起始年", pd.Series(index=df.index, dtype=float)).fillna(df["首篇赛道论文年份"])
    df["学术年龄"] = THIS_YEAR - start
    df["职业阶段"] = pd.cut(df["学术年龄"], [-1, 5, 9, 15, 200],
                        labels=["博士生/博后", "博后末期/青年PI", "PI中期", "资深"]).astype(str)

    if tax:
        career = df.get("生涯AI侧占比", pd.Series(index=df.index, dtype=float))
        df["AI侧占比"] = career.fillna(df["赛道AI侧论文占比"])
        df["类型依据"] = career.notna().map({True: "生涯主题", False: "AI4S论文"})
        if tax["talent_type"].get("mode") == "sector":
            get = lambda c: df.get(c, pd.Series(index=df.index, dtype=object))
            df["类型依据"] = "机构与产业合作"
            df["人才类型"] = [tx.sector_type(a, b, c, tax["talent_type"])
                           for a, b, c in zip(get("当前机构类型"), get("最近加入机构类型"), get("产业合作论文占比"))]
        else:
            df["人才类型"] = [tx.talent_type(v, tax["talent_type"], c == "生涯主题")
                           for v, c in zip(df["AI侧占比"], df["类型依据"])]
        hh = df.get("h指数", pd.Series(index=df.index, dtype=float))
        df["层级"] = [tx.tier(a, b, tax["tiers"]) for a, b in zip(df["学术年龄"], hh)]

    h = df.get("h指数", pd.Series(0, index=df.index)).fillna(0)
    df["学术实力分"] = (0.45 * _pct(df["赛道总被引"].map(math.log1p))
                    + 0.30 * _pct(h)
                    + 0.25 * _pct(df["末位作者数"] + df["通讯作者数"])).round(1)

    momentum = (df["近期论文数"] + 1) / (df["前期论文数"] + 1)
    df["增长势头分"] = (0.5 * _pct(df["近期论文数"]) + 0.5 * _pct(momentum)).round(1)

    df["综合分"] = (0.5 * df["学术实力分"] + 0.5 * df["增长势头分"]).round(1)

    def reasons(r) -> str:
        out = []
        if r["学术实力分"] >= 90:
            out.append("学术实力居全库前 10%")
        if r["增长势头分"] >= 90:
            out.append("近期产出势头居全库前 10%")
        if r["近期论文数"] >= 3 and r["近期论文数"] > r["前期论文数"]:
            out.append(f"近{tcfg['recent_years']}年产出加速（{r['前期论文数']}→{r['近期论文数']}）")
        if (r["末位作者数"] + r["通讯作者数"]) >= 5:
            out.append(f"{int(r['末位作者数'] + r['通讯作者数'])} 篇末位/通讯作者论文")
        return "；".join(out)

    df["推荐理由"] = df.apply(reasons, axis=1)
    return df.sort_values("综合分", ascending=False).reset_index(drop=True)


def institution_table(works: list[dict]) -> pd.DataFrame:
    rec = defaultdict(lambda: {"papers": set(), "authors": set(), "cites": 0})
    meta = {}
    for w in works:
        for a in w.get("authorships") or []:
            for i in a.get("institutions") or []:
                k = i.get("display_name")
                if not k:
                    continue
                meta[k] = (i.get("country_code"), i.get("type"))
                if w["id"] not in rec[k]["papers"]:
                    rec[k]["cites"] += w.get("cited_by_count") or 0
                rec[k]["papers"].add(w["id"])
                rec[k]["authors"].add(_short((a.get("author") or {}).get("id")))
    df = pd.DataFrame([{
        "机构": k, "国家/地区": meta[k][0], "类型": meta[k][1],
        "赛道论文数": len(v["papers"]), "作者数": len(v["authors"]), "总被引": v["cites"],
    } for k, v in rec.items()])
    return df.sort_values("赛道论文数", ascending=False).reset_index(drop=True)
