"""流水线：分领域抓取 → 筛选 → 人才库 → 分类与评分 → 导出 Excel / 可视化数据。

用法：
    python3 run.py                       # 全领域：抓取所有还没抓过的领域，然后重算（额度用完会自动停，次日同一命令续跑）
    python3 run.py --quick               # 试跑：每个领域只抓 200 篇（单独缓存，不影响正式数据）
    python3 run.py --domains 药物发现,计算材料   # 只抓指定领域
    python3 run.py --refresh all         # 重新抓取（all 或逗号分隔的领域名），用于定期更新
    python3 run.py --skip-fetch          # 不调 API，用已缓存数据秒级重算（改了分类关键词/评分后用）
    python3 run.py --skip-fetch --refetch-profiles   # 重新拉全部作者履历（含整个生涯的研究主题，人才类型更准）
"""
from __future__ import annotations

import argparse
import json
import shutil
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import yaml

from src import s2
from src import tracks
from src import taxonomy as tx
from src.rising import domain_ranking, score_rising
from src.classify import abstract_from_inverted, classify_work, hits
from src.openalex_client import BudgetExhausted, OpenAlex, dump_jsonl, load_jsonl
from src.talent import apply_recent_papers, build_author_table, enrich_with_author_profiles, institution_table, score

ROOT = Path(__file__).parent
DATA = ROOT / "data"
OUT = ROOT / "output"
APP_DIR = OUT / "app_data"                 # 当前赛道的可视化数据目录（main 里按 --track 设置）
LEGACY = DATA / "raw_works.jsonl"          # 旧版单赛道（蛋白/抗体）数据
PROFILES = DATA / "author_profiles.jsonl"
BUDGET_MSG = ("\n⚠️  OpenAlex 今日额度已用完（HTTP 429）。已抓完的部分都已保存。\n"
              "   明天再运行同一条命令，会从没抓完的领域继续；现在先用已有数据生成结果。\n")


def _short(oa_id: str) -> str:
    return oa_id.rsplit("/", 1)[-1]


def slim(w: dict) -> dict:
    """只保留流水线用到的字段，摘要从倒排索引还原成正文。体积和内存约降到原来的 1/4。"""
    if w.get("_slim"):
        return w
    src = ((w.get("primary_location") or {}).get("source") or {}).get("display_name")
    field = ((w.get("primary_topic") or {}).get("field") or {}).get("display_name")
    out = {k: w.get(k) for k in ("id", "doi", "title", "publication_year", "publication_date",
                                 "type", "cited_by_count")}
    out.update({
        "abstract": w.get("abstract") or abstract_from_inverted(w.get("abstract_inverted_index")),
        "primary_location": {"source": {"display_name": src}},
        "primary_topic": {"field": {"display_name": field}},
        "authorships": [{
            "author": {k: (a.get("author") or {}).get(k) for k in ("id", "display_name", "orcid")},
            "author_position": a.get("author_position"),
            "is_corresponding": a.get("is_corresponding"),
            "institutions": [{k: i.get(k) for k in ("display_name", "country_code", "type")}
                             for i in a.get("institutions") or []],
        } for a in w.get("authorships") or []],
        "_fetch_domains": w.get("_fetch_domains"),
        "_fetch_pass": w.get("_fetch_pass", "cited"),
        "_slim": True,
    })
    return out


def iter_cache(path: Path):
    """逐行读取缓存并精简，避免一次性把大文件全部载入内存。"""
    with path.open(encoding="utf-8") as f:
        for line in f:
            if line.strip():
                yield slim(json.loads(line))


def cache_path(domain: str, quick: bool) -> Path:
    return DATA / ("works_quick" if quick else "works") / f"{domain.replace('/', '-')}.jsonl"


def _api(cfg: dict) -> OpenAlex:
    oc = cfg["openalex"]
    return OpenAlex(api_key=oc.get("api_key") or "", mailto=oc.get("mailto") or "")


def fetch_domain(api: OpenAlex, cfg: dict, domain: str, quick: bool, old: list[dict] | None = None) -> list[dict]:
    """old：这个领域上一次的缓存。其中仍在时间窗口内的「最新论文」会保留下来，
    每周更新时逐周累积，最终覆盖完整的 newest_months 个月（每个领域最多 newest_keep 篇）。"""
    fc, oc = cfg["fetch"], cfg["openalex"]
    q = tracks.query(fc, domain)
    recent_from = f"{date.today().year - fc['recent_years'] + 1}-01-01"
    newest_from = (date.today() - timedelta(days=round(30.4 * fc.get("newest_months", 6)))).isoformat()
    passes = [("高被引", fc["from_date"], 100 if quick else fc["top_cited"], "cited_by_count:desc"),
              (f"近{fc['recent_years']}年", recent_from, 100 if quick else fc["recent"], "cited_by_count:desc")]
    if fc.get("newest"):
        # 按发表时间倒序、不看被引：让刚发表、还没被引用的新人新作也能进来
        passes.append((f"近{fc.get('newest_months', 6)}个月最新", newest_from,
                       100 if quick else fc["newest"], "publication_date:desc"))
    seen, works = set(), []
    for name, since, cap, sort in passes:
        n0 = len(works)
        tag = "newest" if sort.startswith("publication_date") else "cited"
        for w in api.iter_works(q, since, oc["types"], oc["per_page"], cap, sort=sort,
                                to_date=date.today().isoformat()):
            if w["id"] not in seen:
                seen.add(w["id"])
                w["_fetch_domains"] = [domain]
                w["_fetch_pass"] = tag
                works.append(slim(w))
        print(f"    {name}：新增 {len(works) - n0} 篇")

    # 滚动累积「最新论文」：保留上次抓到、仍在窗口内的，按发表日期取最新的 newest_keep 篇
    if fc.get("newest") and old:
        kept_old = [w for w in old if w.get("_fetch_pass") == "newest" and w["id"] not in seen
                    and (w.get("publication_date") or "") >= newest_from]
        newest = [w for w in works if w["_fetch_pass"] == "newest"] + kept_old
        newest.sort(key=lambda w: w.get("publication_date") or "", reverse=True)
        keep = newest[:fc.get("newest_keep", 5000)]
        works = [w for w in works if w["_fetch_pass"] != "newest"] + keep
        old_ids = {w["id"] for w in kept_old}
        print(f"    最新论文累积：保留上次 {sum(w['id'] in old_ids for w in keep)} 篇，"
              f"窗口内共 {len(keep)} 篇（覆盖到 {keep[-1].get('publication_date') if keep else '—'}）")
    return works


def domain_growth(cfg: dict, allow_api: bool, refresh: bool) -> dict[str, float]:
    """各领域近两年 vs 前两年的论文数增长率（OpenAlex 全量计数，不受抓取上限影响）。缓存在 data/domain_counts.json。"""
    path = DATA / "domain_counts.json"
    counts = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    todo = [d for d in cfg["fetch"]["domains"] if refresh or d not in counts]
    if todo and allow_api:
        api = _api(cfg)
        try:
            for d in todo:
                counts[d] = api.count_by_year(tracks.query(cfg["fetch"], d), cfg["fetch"]["from_date"],
                                              cfg["openalex"]["types"])
        except BudgetExhausted:
            print(BUDGET_MSG)
        path.write_text(json.dumps(counts, ensure_ascii=False), encoding="utf-8")
    y = date.today().year
    out = {}
    for d in cfg["fetch"]["domains"]:
        c = {int(k): v for k, v in (counts.get(d) or {}).items()}
        if c:   # 今年还没过完，用「去年 + 前年」对比「再往前两年」
            recent, prev = c.get(y - 1, 0) + c.get(y - 2, 0), c.get(y - 3, 0) + c.get(y - 4, 0)
            out[d] = round((recent + 1) / (prev + 1) - 1, 3)
    for sub, src in ((cfg.get("taxonomy") or {}).get("fetch_alias") or {}).items():   # 二级领域名和检索领域名不同时
        vals = [out[x] for x in ([src] if isinstance(src, str) else src) if x in out]
        if vals and sub not in out:
            out[sub] = round(sum(vals) / len(vals), 3)
    return out


def fetch_all(cfg: dict, quick: bool, only: list[str] | None, refresh: list[str]) -> list[str]:
    """逐个领域抓取并缓存；返回还没抓完的领域。"""
    domains = list(cfg["fetch"]["domains"])
    targets = [d for d in domains if not only or d in only]
    # 旧版蛋白赛道数据直接沿用为「蛋白与核酸设计」领域的缓存，省额度
    prot = cache_path("蛋白与核酸设计", False)
    if not quick and LEGACY.exists() and not prot.exists() and "蛋白与核酸设计" in domains \
            and "蛋白与核酸设计" not in refresh and "all" not in refresh:
        prot.parent.mkdir(parents=True, exist_ok=True)
        ws = load_jsonl(LEGACY)
        for w in ws:
            w["_fetch_domains"] = ["蛋白与核酸设计"]
        dump_jsonl(ws, prot)
        print(f"[迁移] 旧版蛋白/抗体赛道的 {len(ws)} 篇论文沿用为「蛋白与核酸设计」领域数据"
              "（想按新口径重抓：--refresh 蛋白与核酸设计）")
    todo = [d for d in targets if not cache_path(d, quick).exists() or "all" in refresh or d in refresh]
    if not todo:
        print("[抓取] 所有领域都已缓存，跳过（需要更新时加 --refresh all）")
        return []
    api = _api(cfg)
    for i, d in enumerate(todo, 1):
        print(f"[抓取 {i}/{len(todo)}] {d}")
        p = cache_path(d, quick)
        old = list(iter_cache(p)) if p.exists() else None
        try:
            works = fetch_domain(api, cfg, d, quick, old)
        except BudgetExhausted:
            print(BUDGET_MSG)
            return todo[i - 1:]
        dump_jsonl(works, cache_path(d, quick))
        print(f"    已保存 {len(works)} 篇（累计请求 {api.requests} 次）")
    return []


def load_works(cfg: dict, quick: bool) -> list[dict]:
    """合并所有领域缓存；同一篇论文被多个领域抓到时合并来源。没有分领域缓存时退回旧版数据。"""
    merged: dict[str, dict] = {}
    for d in cfg["fetch"]["domains"]:
        p = cache_path(d, quick)
        if not p.exists():
            continue
        for w in iter_cache(p):
            if w["id"] in merged:
                old = merged[w["id"]]["_fetch_domains"]
                old += [x for x in w.get("_fetch_domains", [d]) if x not in old]
            else:
                w.setdefault("_fetch_domains", [d])
                merged[w["id"]] = w
    if not merged and LEGACY.exists() and not quick:
        print("[数据] 还没有分领域数据，先用旧版蛋白/抗体赛道数据")
        return list(iter_cache(LEGACY))
    return list(merged.values())


def add_citation_percentiles(kept: list[dict], labels: dict, min_group: int = 20):
    """给每篇论文算「同年、同领域论文中的被引百分位」（0–100）。

    新论文只和同一年发表的论文比，因此刚发表一两年的好论文也能拿到高百分位，不会被老论文压住。
    同年同领域论文少于 min_group 篇时，退回到只按年份比较。
    """
    if not kept:
        return
    df = pd.DataFrame([{
        "wid": _short(w["id"]), "year": w.get("publication_year") or 0,
        "dom": (labels[_short(w["id"])].get("科学领域") or ["未归类"])[0],
        "cites": w.get("cited_by_count") or 0,
    } for w in kept])
    by_dom = df.groupby(["year", "dom"])["cites"]
    pct = by_dom.rank(pct=True, method="average") * 100
    small = by_dom.transform("size") < min_group
    year_pct = df.groupby("year")["cites"].rank(pct=True, method="average") * 100
    pct[small] = year_pct[small]
    for wid, p in zip(df["wid"], pct.round(1)):
        labels[wid]["被引百分位"] = float(p)


def enrich(cfg: dict, authors: pd.DataFrame, tcfg: dict, allow_api: bool, refetch: bool) -> list[dict]:
    """作者履历增量补全：已缓存的不重复拉；额度用完时保留已拉到的。"""
    cache = {_short(p["id"]): p for p in load_jsonl(PROFILES)} if PROFILES.exists() else {}
    if allow_api or refetch:
        top = score(authors, tcfg).head(tcfg["top_n_enrich"])["author_id"].tolist()
        # 新星候选也补全履历（学术年龄、h 指数要靠履历才准），按主导论文影响力取前 N 位
        rs = cfg.get("rising_star") or {}
        cand = (authors[authors["近期论文数"] >= rs.get("min_recent_papers", 2)]
                .sort_values(["影响力百分位", "近期末位或通讯数"], ascending=False)
                .head(rs.get("enrich_candidates", 3000))["author_id"].tolist())
        top = list(dict.fromkeys(top + cand))
        top = [a for a in top if not str(a).startswith("S2-")]   # S2 人员的履历由 Semantic Scholar 补全
        need = top if refetch else [a for a in top if a not in cache]
        if need:
            print(f"[补全] 拉取 {len(need)} 位作者的完整履历（已缓存 {len(cache)} 位）…")
            try:
                for p in _api(cfg).get_authors(need):
                    cache[_short(p["id"])] = p
            except BudgetExhausted:
                print(BUDGET_MSG)
            dump_jsonl(cache.values(), PROFILES)
    else:
        print(f"[补全] 复用缓存的 {len(cache)} 份作者履历（不调 API）")
    return list(cache.values())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-fetch", action="store_true", help="不调 API，只用缓存数据重算")
    ap.add_argument("--quick", action="store_true", help="试跑：每个领域 200 篇，单独缓存")
    ap.add_argument("--domains", default="", help="只抓这些领域，逗号分隔；science=全部科学领域，ai=全部通用 AI 方向")
    ap.add_argument("--refresh", default="", help="重新抓取：all、science、ai 或逗号分隔的领域名")
    ap.add_argument("--no-enrich", action="store_true", help="跳过作者详情补全")
    ap.add_argument("--refetch-profiles", action="store_true", help="重新拉取全部作者履历（含研究主题分布）")
    ap.add_argument("--no-s2", action="store_true", help="不使用 Semantic Scholar 补充数据")
    ap.add_argument("--track", default=tracks.DEFAULT,
                    help="赛道：ai4s（默认）或 config.yaml 里 tracks 段定义的其他赛道，如 general_ai")
    args = ap.parse_args()

    cfg = tracks.apply(yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8")), args.track)
    global APP_DIR
    APP_DIR = tracks.app_dir(ROOT, args.track)
    print(f"[赛道] {cfg.get('track_name')}（结果写入 {APP_DIR.relative_to(ROOT)}）")
    tax = cfg.get("taxonomy")
    split = lambda s: [x.strip() for x in s.replace("，", ",").split(",") if x.strip()]
    only, refresh = split(args.domains), split(args.refresh)
    groups = {"science": [d for d in cfg["fetch"]["domains"] if not tracks.is_ai_domain(cfg["fetch"], d)],
              "ai": [d for d in cfg["fetch"]["domains"] if tracks.is_ai_domain(cfg["fetch"], d)]}
    expand = lambda xs: list(dict.fromkeys(y for x in xs for y in groups.get(x, [x])))
    only, refresh = expand(only), expand(refresh)    # science = 全部科学领域，ai = 全部通用 AI 方向
    unknown = [d for d in only + refresh if d != "all" and d not in cfg["fetch"]["domains"]]
    if unknown:
        raise SystemExit(f"未知领域：{unknown}。可选：{list(cfg['fetch']['domains'])}")

    pending = [] if args.skip_fetch else fetch_all(cfg, args.quick, only, refresh)
    works = load_works(cfg, args.quick)
    done = {d for w in works for d in w.get("_fetch_domains", [])}
    print(f"[数据] {len(works)} 篇论文，覆盖 {len(done)}/{len(cfg['fetch']['domains'])} 个领域")
    use_s2 = (cfg.get("s2") or {}).get("enabled", True) and not args.no_s2
    if use_s2:
        if not s2.resolve_key():
            print("[S2] 提示：没有 Semantic Scholar API Key，会走共享限额、经常被限速，速度会慢很多（见 README）")
        extra = s2.supplement_works(cfg, works, args.quick, allow_api=not args.skip_fetch, refresh=refresh)
        print(f"[S2] 补充 {len(extra)} 篇 OpenAlex 里没有的论文")
        works += extra

    # 1. 筛选：命中 AI 方法词，且属于分类体系里的某个科学领域
    labels, kept = {}, []
    for w in works:
        lab = classify_work(w, cfg["classifier"])
        field = ((w.get("primary_topic") or {}).get("field") or {}).get("display_name")
        venue = ((w.get("primary_location") or {}).get("source") or {}).get("display_name")
        seeds = w.get("_fetch_domains") or []
        ai_only = bool(seeds) and all(tracks.is_ai_domain(cfg["fetch"], d) for d in seeds)   # 只被通用 AI 方向的检索抓到
        lab.update(tx.tag_work(w.get("title"), w.get("abstract") or abstract_from_inverted(w.get("abstract_inverted_index")),
                               field, tax, venue, seed_domains=seeds, strict=ai_only))
        # 必须属于某个领域；通用 AI 方向抓到、但哪个领域都没命中的论文也保留（keep_unclassified），记为「未归类」
        in_scope = bool(lab["科学领域"]) or (bool(tax.get("keep_unclassified")) and ai_only)
        lab["relevant"] = bool(lab["ai_hits"]) and in_scope
        labels[_short(w["id"])] = lab
        if lab["relevant"]:
            kept.append(w)
    print(f"[筛选] {len(works)} → {len(kept)} 篇判定为本赛道论文")

    # 1.5 用 Semantic Scholar 修正作者归属、合并同一人的多个作者号
    people, aliases = {}, {}
    if use_s2:
        n_oa = sum(w.get("_source") != "S2" for w in kept)
        matches = s2.match_papers(cfg, kept, allow_api=not args.skip_fetch)
        print(f"[S2] {len(matches)}/{n_oa} 篇 OpenAlex 论文对齐到 S2，改用 S2 的完整作者列表")
        people, aliases = s2.unify(kept, matches, cfg, allow_api=not args.skip_fetch)
        merged = sum(len(p["s2_ids"]) > 1 for p in people.values())
        print(f"[S2] 识别出 {len(people)} 位作者，其中 {merged} 位由多个作者号合并而来")
    add_citation_percentiles(kept, labels)

    # 2. 人才库
    tcfg = cfg["talent"]
    authors = build_author_table(kept, labels, tcfg, tax)
    print(f"[人才库] {len(authors)} 位作者（赛道论文 ≥ {tcfg['min_track_papers']}）")

    # 3. 补全作者履历（对初排前 N 名，增量）
    if not args.no_enrich and len(authors):
        profiles = enrich(cfg, authors, tcfg, allow_api=not args.skip_fetch, refetch=args.refetch_profiles)
        if people:
            rs = cfg.get("rising_star") or {}
            pre = score(authors, tcfg)
            cand = (authors[authors["近期论文数"] >= rs.get("min_recent_papers", 2)]
                    .sort_values(["影响力百分位", "近期末位或通讯数"], ascending=False)
                    .head(rs.get("enrich_candidates", 3000))["author_id"].tolist())
            wanted = [a for a in dict.fromkeys(pre.head(tcfg["top_n_enrich"])["author_id"].tolist() + cand)
                      if str(a).startswith("S2-")]
            everyone = [a for a in authors["author_id"] if str(a).startswith("S2-")]
            profiles += s2.author_profiles(cfg, people, everyone, allow_api=not args.skip_fetch, years_for=wanted)
            if tax:   # 「当前方向」：再看这些人最近两年的全部论文（不限于人才库的检索范围）
                recent = s2.recent_papers(cfg, people, everyone, allow_api=not args.skip_fetch)   # 人才库里所有人
                authors = apply_recent_papers(authors, recent, tax)
                print(f"[当前方向] {len(recent)} 位作者用最近两年的全部论文校正了细分领域")
        authors = enrich_with_author_profiles(authors, profiles, tax)

    authors = authors.drop(columns=[c for c in ["_dw", "_dr", "_tn"] if c in authors])
    ranked = score(authors, tcfg, tax)
    ranked = score_rising(ranked, cfg.get("rising_star"), tcfg["recent_years"])
    print(f"[新星] {int(ranked['新星资格'].sum())} 位学术新星进入分领域新星榜")
    insts = institution_table(kept)
    countries = (ranked.groupby("国家/地区")
                 .agg(人才数=("author_id", "size"), 综合分均值=("综合分", "mean"),
                      Top200人数=("综合分", lambda s: int((s.index < 200).sum())))
                 .sort_values("人才数", ascending=False).round(1).reset_index())

    papers = pd.DataFrame([{
        "标题": w.get("title"), "年份": w.get("publication_year"),
        "被引": w.get("cited_by_count"), "DOI": w.get("doi"),
        "期刊/平台": ((w.get("primary_location") or {}).get("source") or {}).get("display_name"),
        "置信度": labels[w["id"].rsplit("/", 1)[-1]]["confidence"],
        "科学领域": "、".join(labels[w["id"].rsplit("/", 1)[-1]].get("科学领域", [])),
        "同年同领域被引百分位": labels[w["id"].rsplit("/", 1)[-1]].get("被引百分位"),
        "抓取来源领域": "、".join(w.get("_fetch_domains", [])),
        "OpenAlex": w["id"],
    } for w in kept]).sort_values("被引", ascending=False)

    # 4. 导出
    OUT.mkdir(exist_ok=True)
    safe = cfg["track_name"].replace("/", "-")
    path = OUT / f"人才线索_{safe}_{date.today():%Y%m%d}.xlsx"
    lead_cols = ["综合分", "姓名", "大类", "层级", "主学科", "副学科",
                 "推荐理由", "当前机构", "国家/地区", "职业阶段",
                 "学术实力分", "增长势头分", "赛道论文数", "近期论文数",
                 "赛道总被引", "h指数", "末位作者数",
                 "最近加入机构", "最近加入年份", "完整机构履历", "常合作者",
                 "代表作", "代表作被引", "ORCID", "author_id"]
    lead_cols = [c for c in lead_cols if c in ranked.columns]
    star_cols = ["大类", "主学科", "领域排名", "新星分", "姓名", "新星理由", "当前机构", "国家/地区",
                 "学术年龄", "层级", "影响力分", "势头分", "独立性分",
                 "近期论文数", "近期末位或通讯数", "近期第一作者数", "影响力百分位", "近期最佳论文",
                 "最近加入机构", "最近加入年份", "ORCID", "author_id"]
    stars = domain_ranking(ranked, "主学科", (cfg.get("rising_star") or {}).get("top_n_per_domain", 30))
    if tax:   # 按分类体系里的领域顺序排列
        order = {d: i for i, d in enumerate(tx.domain_order(tax))}
        stars = stars.sort_values(["主学科", "领域排名"], key=lambda s: s.map(order) if s.name == "主学科" else s)
    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        xl = {"主学科": "领域", "副学科": "副领域"}
        stars[[c for c in star_cols if c in stars]].rename(columns=xl).to_excel(xw, sheet_name="新星榜", index=False)
        ranked[lead_cols].rename(columns=xl).to_excel(xw, sheet_name="人才线索榜", index=False)
        insts.head(500).to_excel(xw, sheet_name="机构榜", index=False)
        countries.to_excel(xw, sheet_name="国家分布", index=False)
        papers.to_excel(xw, sheet_name="赛道论文", index=False)
        if tax:
            pd.crosstab(ranked["层级"], ranked["大类"], margins=True, margins_name="合计") \
                .to_excel(xw, sheet_name="大类×层级")
        for ws in xw.book.worksheets:
            ws.freeze_panes = {"人才线索榜": "C2", "新星榜": "F2"}.get(ws.title, "A2")
            for col in ws.columns:
                width = max(len(str(c.value or "")) for c in list(col)[:50])
                ws.column_dimensions[col[0].column_letter].width = min(max(8, width * 1.1), 50)
    print(f"[完成] {path}")

    # 5. 给可视化软件用的数据（app.py 读取）
    export_app_data(ranked, insts, kept, labels)
    if aliases:   # 旧 OpenAlex 作者号 → 新人员号，软件据此把以前的跟进记录对到同一个人
        pd.DataFrame(list(aliases.items()), columns=["old_id", "new_id"]).to_csv(
            APP_DIR / "id_aliases.csv", index=False)
    print("[可视化] 数据已就绪，运行：streamlit run app.py")
    if pending:
        again = "python3 run.py" + ("" if args.track == tracks.DEFAULT else f" --track {args.track}")
        print(f"[待续] 还有 {len(pending)} 个领域没抓：{'、'.join(pending)}。明天再运行 {again} 即可续跑。")


def export_app_data(ranked: pd.DataFrame, insts: pd.DataFrame, kept: list[dict], labels: dict):
    d = APP_DIR
    d.mkdir(parents=True, exist_ok=True)
    ranked.to_csv(d / "talents.csv", index=False)
    insts.to_csv(d / "institutions.csv", index=False)
    prow, arow = [], []
    for w in kept:
        wid = w["id"].rsplit("/", 1)[-1]
        prow.append({
            "work_id": wid, "标题": w.get("title"), "年份": w.get("publication_year"),
            "被引": w.get("cited_by_count"), "DOI": w.get("doi"),
            "期刊/平台": ((w.get("primary_location") or {}).get("source") or {}).get("display_name"),
            "置信度": labels[wid]["confidence"], "子方向": "|".join(labels[wid]["subtopics"]),
            "同年同领域被引百分位": labels[wid].get("被引百分位"),
            "科学领域": "|".join(labels[wid].get("科学领域", [])),
            "AI方法": "|".join(labels[wid].get("AI方法", [])),
        })
        for a in w.get("authorships") or []:
            au = a.get("author") or {}
            inst = (a.get("institutions") or [{}])[0]
            arow.append({
                "work_id": wid, "author_id": (au.get("id") or "").rsplit("/", 1)[-1],
                "姓名": au.get("display_name"), "位置": a.get("author_position"),
                "机构": inst.get("display_name"), "机构类型": inst.get("type"),
                "国家/地区": inst.get("country_code"), "年份": w.get("publication_year"),
            })
    pd.DataFrame(prow).to_csv(d / "papers.csv", index=False)
    pd.DataFrame(arow).to_csv(d / "authorships.csv", index=False)


if __name__ == "__main__":
    main()
