"""Semantic Scholar（S2）补充数据源。

OpenAlex 对 AI / 计算机方向、名字常见的中国学者识别很差：论文没挂作者号、被并到同名的人名下、
上百人的大团队论文只保留前 100 位作者。S2 在这些方面明显更好，而且很多作者档案带有 DBLP 的
消歧标识（例如 "Kai Chen 0026"），可以把被拆开的同一个人重新合并。

本模块做三件事：
1. 补抓：按领域在 S2 检索（被引最高 + 最新），把 OpenAlex 漏掉的论文补进来；
2. 归属修正：用 DOI / arXiv 号把 OpenAlex 论文对到 S2，改用 S2 的完整作者列表和作者号；
3. 身份合并：同一 DBLP 消歧标识或同一 ORCID 的 S2 作者号合并成一个人（作者号记为 "S2-<id>"）。

API Key（可选，强烈建议）：环境变量 S2_API_KEY，或项目里的 .s2_key 文件。
免费申请：https://www.semanticscholar.org/product/api#api-key-form
没有 Key 时与全球用户共享限额，经常被限速，会自动等待重试，但会慢很多。
"""
from __future__ import annotations

import json
import os
import re
import time
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable

import requests

BASE = "https://api.semanticscholar.org/graph/v1"
ROOT = Path(__file__).resolve().parent.parent
KEY_FILE = ROOT / ".s2_key"
S2_DIR = ROOT / "data" / "s2"
PAPER_FIELDS = "paperId,externalIds,title,abstract,year,publicationDate,citationCount,venue,fieldsOfStudy,publicationTypes,authors"
MATCH_FIELDS = "paperId,externalIds,citationCount,authors"
AUTHOR_FIELDS = "name,externalIds,affiliations,hIndex,paperCount,citationCount"
DBLP_DISAMBIGUATED = re.compile(r"\s\d{4}$")     # "Kai Chen 0026" 这类带编号的才是唯一身份


class S2Unavailable(RuntimeError):
    """多次重试仍被限速或连不上。调用方保存已有进度后继续用缓存。"""


def resolve_key() -> str:
    key = os.environ.get("S2_API_KEY", "").strip()
    if not key and KEY_FILE.exists():
        key = KEY_FILE.read_text(encoding="utf-8").strip()
    return key


class S2Client:
    def __init__(self, pause: float | None = None, timeout: int = 60):
        self.key = resolve_key()
        self.s = requests.Session()
        if self.key:
            self.s.headers["x-api-key"] = self.key
        # 有 Key 默认 1 次/秒；无 Key 走共享限额，放慢一些
        self.pause = pause if pause is not None else (1.2 if self.key else 3.0)
        self.timeout = timeout
        self.requests = 0
        self._last = 0.0

    def _call(self, method: str, path: str, params: dict, body: dict | None = None):
        """限速（429）时按服务器要求等待后继续，最多等约 10 分钟；网络错误重试 6 次。"""
        limited, errors = 0, 0
        while True:
            wait = self.pause - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()
            try:
                r = self.s.request(method, f"{BASE}{path}", params=params, json=body, timeout=self.timeout)
            except requests.RequestException as e:
                errors += 1
                if errors > 6:
                    raise S2Unavailable(f"连不上 Semantic Scholar（{e}）")
                print(f"    [S2 网络波动] {type(e).__name__}，{5 * errors}s 后重试（正常现象，不用处理）")
                time.sleep(5 * errors)
                continue
            if r.status_code == 429:
                limited += 1
                if limited > 20:
                    raise S2Unavailable("Semantic Scholar 持续限速，先保存进度，稍后再运行会接着拉")
                retry = r.headers.get("Retry-After", "")
                backoff = int(retry) if retry.isdigit() else min(60, 5 * limited)
                if limited == 1 or limited % 5 == 0:
                    print(f"    [S2 限速] 请求太快被暂时限流，等 {backoff}s 后自动继续（正常现象，不用处理）")
                time.sleep(backoff)
                self.pause = min(3.0, self.pause * 1.2)   # 被限流后自动放慢
                continue
            if r.status_code >= 500:
                errors += 1
                if errors > 6:
                    raise S2Unavailable(f"Semantic Scholar 服务器错误（HTTP {r.status_code}）")
                time.sleep(5 * errors)
                continue
            if r.status_code == 403:
                raise S2Unavailable("S2 拒绝访问（HTTP 403）：API Key 无效？")
            if r.status_code >= 400:
                raise S2Unavailable(f"Semantic Scholar 拒绝了请求（HTTP {r.status_code}）：{r.text[:200]}")
            self.requests += 1
            return r.json()

    def batch(self, kind: str, ids: list[str], fields: str, size: int) -> Iterable[tuple[str, dict | None]]:
        for k in range(0, len(ids), size):
            chunk = ids[k:k + size]
            data = self._call("POST", f"/{kind}/batch", {"fields": fields}, {"ids": chunk})
            yield from zip(chunk, data)

    def bulk_search(self, query: str, since: str, sort: str, max_n: int) -> list[dict]:
        params = {"query": query, "fields": PAPER_FIELDS, "publicationDateOrYear": f"{since}:",
                  "sort": sort}
        out, token = [], None
        while len(out) < max_n:
            if token:
                params["token"] = token
            data = self._call("GET", "/paper/search/bulk", params)
            out += data.get("data") or []
            token = data.get("token")
            if not token:
                break
        return out[:max_n]


# ------------------------------------------------------------------ 缓存
def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as f:
        return {k: v for line in f if line.strip() for k, v in [json.loads(line)]}


def _append(path: Path, items: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for k, v in items.items():
            f.write(json.dumps([k, v], ensure_ascii=False) + "\n")


# ------------------------------------------------------------------ 工具
def to_s2_query(oa_query: str) -> str:
    """OpenAlex 布尔检索式 → S2 语法（AND → +，OR → |）。"""
    return oa_query.replace(" AND ", " + ").replace(" OR ", " | ")


def norm_doi(doi: str | None) -> str:
    return re.sub(r"^https?://(dx\.)?doi\.org/", "", (doi or "").strip(), flags=re.I).lower()


def s2_id_for(work: dict) -> str | None:
    """OpenAlex 论文在 S2 批量接口里的标识：arXiv 论文用 ARXIV:号，其余用 DOI:。"""
    doi = norm_doi(work.get("doi"))
    if not doi:
        return None
    m = re.match(r"10\.48550/arxiv\.(.+)$", doi)
    return f"ARXIV:{m.group(1)}" if m else f"DOI:{doi}"


def norm_title(t: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (t or "").lower())[:120]


def name_keys(name: str | None) -> set[str]:
    """姓名的几种写法，用来在 OpenAlex 和 S2 的作者列表之间对人。"""
    toks = [t for t in re.split(r"[^a-z]+", (name or "").lower()) if t]
    if not toks:
        return set()
    first, last = toks[0], toks[-1]
    return {" ".join(sorted(toks)), f"{last} {first[0]}", f"{first} {last[0]}", "".join(sorted(toks))}


# ------------------------------------------------------------------ 1. 补抓
def search_domain(client: S2Client, cfg: dict, domain: str, quick: bool) -> list[dict]:
    fc, sc = cfg["fetch"], cfg.get("s2") or {}
    from src.tracks import query
    q = to_s2_query(query(fc, domain))
    newest_from = (date.today() - timedelta(days=round(30.4 * sc.get("newest_months", 6)))).isoformat()
    passes = [("cited", fc["from_date"], "citationCount:desc", 100 if quick else sc.get("top_cited", 2000)),
              ("newest", newest_from, "publicationDate:desc", 100 if quick else sc.get("newest", 1000))]
    seen, out = set(), []
    for tag, since, sort, cap in passes:
        if cap <= 0:
            continue
        for p in client.bulk_search(q, since, sort, cap):
            if p.get("paperId") and p["paperId"] not in seen:
                seen.add(p["paperId"])
                p["_fetch_pass"] = tag
                out.append(p)
    return out


TYPE_MAP = {"Review": "review", "Conference": "conference-paper", "JournalArticle": "article"}


def s2_to_work(p: dict, domain: str) -> dict:
    """S2 论文 → 流水线使用的（精简）论文格式。"""
    ext = p.get("externalIds") or {}
    types = p.get("publicationTypes") or []
    kind = next((TYPE_MAP[t] for t in types if t in TYPE_MAP), "preprint" if ext.get("ArXiv") else "article")
    doi = ext.get("DOI") or (f"10.48550/arXiv.{ext['ArXiv']}" if ext.get("ArXiv") else None)
    return {
        "id": f"https://www.semanticscholar.org/paper/{p['paperId']}",
        "doi": f"https://doi.org/{doi}" if doi else None,
        "title": p.get("title"), "abstract": p.get("abstract") or "",
        "publication_year": p.get("year"), "publication_date": p.get("publicationDate") or (
            f"{p['year']}-01-01" if p.get("year") else None),
        "type": kind, "cited_by_count": p.get("citationCount") or 0,
        "primary_location": {"source": {"display_name": p.get("venue") or ("arXiv" if ext.get("ArXiv") else None)}},
        "primary_topic": {"field": {"display_name": (p.get("fieldsOfStudy") or [None])[0]}},
        "authorships": [{"author": {"id": f"S2:{a.get('authorId')}" if a.get("authorId") else None,
                                    "display_name": a.get("name"), "orcid": None},
                         "author_position": "first" if i == 0 else (
                             "last" if i == len(p.get("authors") or []) - 1 else "middle"),
                         "is_corresponding": False, "institutions": []}
                        for i, a in enumerate(p.get("authors") or [])],
        "_fetch_domains": [domain], "_fetch_pass": p.get("_fetch_pass", "cited"),
        "_source": "S2", "_slim": True,
    }


def supplement_works(cfg: dict, oa_works: list[dict], quick: bool, allow_api: bool,
                     refresh: list[str]) -> list[dict]:
    """按领域从 S2 补抓论文，去掉 OpenAlex 里已有的，返回新增论文。"""
    sdir = S2_DIR / ("search_quick" if quick else "search")
    have_doi = {norm_doi(w.get("doi")) for w in oa_works if w.get("doi")}
    have_title = {norm_title(w.get("title")) for w in oa_works}
    client = None
    added: dict[str, dict] = {}
    for d in cfg["fetch"]["domains"]:
        path = sdir / f"{d.replace('/', '-')}.jsonl"
        need = allow_api and (not path.exists() or "all" in refresh or d in refresh)
        if need:
            client = client or S2Client((cfg.get("s2") or {}).get("pause"))
            print(f"[S2 补抓] {d}")
            try:
                papers = search_domain(client, cfg, d, quick)
            except S2Unavailable as e:
                print(f"    {e}  先跳过，下次运行再补")
                papers = None
            if papers is not None:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("\n".join(json.dumps(p, ensure_ascii=False) for p in papers), encoding="utf-8")
        if not path.exists():
            continue
        # 注意：不能用 splitlines()——摘要里的特殊换行符（U+2028 等）会被它当成换行，把一条记录切断
        for line in path.read_text(encoding="utf-8").split("\n"):
            if not line.strip():
                continue
            try:
                p = json.loads(line)
            except json.JSONDecodeError:
                continue   # 个别损坏的行（例如上次写入时被中断）直接跳过
            ext = p.get("externalIds") or {}
            doi = (ext.get("DOI") or "").lower()
            arx = f"10.48550/arxiv.{ext['ArXiv']}".lower() if ext.get("ArXiv") else ""
            if (doi and doi in have_doi) or (arx and arx in have_doi) or norm_title(p.get("title")) in have_title:
                continue
            w = s2_to_work(p, d)
            if w["id"] in added:
                added[w["id"]]["_fetch_domains"].append(d)
            else:
                added[w["id"]] = w
    return list(added.values())


# ------------------------------------------------------------------ 2/3. 归属修正与身份合并
def match_papers(cfg: dict, works: list[dict], allow_api: bool) -> dict[str, dict]:
    """OpenAlex 论文 → S2 论文（作者列表、作者号、被引）。结果缓存，已查过的不再查。"""
    path = S2_DIR / "paper_match.jsonl"
    cache = _load(path)
    todo = {}
    for w in works:
        if w.get("_source") == "S2":
            continue
        sid = s2_id_for(w)
        if sid and sid not in cache:
            todo[sid] = None
    if todo and allow_api:
        print(f"[S2 对齐] 用 DOI/arXiv 号匹配 {len(todo)} 篇论文（已缓存 {len(cache)} 篇）…")
        client = S2Client((cfg.get("s2") or {}).get("pause"))
        buf = {}
        try:
            for sid, p in client.batch("paper", list(todo), MATCH_FIELDS, 500):
                buf[sid] = {"paperId": p["paperId"], "citationCount": p.get("citationCount"),
                            "authors": p.get("authors") or []} if p else None
                if len(buf) >= 2000:
                    _append(path, buf), cache.update(buf), buf.clear()
        except S2Unavailable as e:
            print(f"    {e}  已匹配的部分已保存，下次运行继续")
        _append(path, buf), cache.update(buf)
    out = {}
    for w in works:
        sid = s2_id_for(w) if w.get("_source") != "S2" else None
        if sid and cache.get(sid):
            out[w["id"]] = cache[sid]
    return out


def fetch_author_details(cfg: dict, ids: Iterable[str], allow_api: bool) -> dict[str, dict]:
    path = S2_DIR / "authors.jsonl"
    cache = _load(path)
    todo = [i for i in dict.fromkeys(ids) if i and i not in cache]
    if todo and allow_api:
        print(f"[S2 作者] 拉取 {len(todo)} 位作者的档案（用于合并同一人的多个作者号；已缓存 {len(cache)} 位）…")
        client = S2Client((cfg.get("s2") or {}).get("pause"))
        buf = {}
        try:
            done = 0
            for aid, a in client.batch("author", todo, AUTHOR_FIELDS, 1000):
                buf[aid] = a or {}
                done += 1
                if len(buf) >= 5000:
                    _append(path, buf), cache.update(buf), buf.clear()
                    print(f"    已拉取 {done:,} / {len(todo):,} 位")
        except S2Unavailable as e:
            print(f"    {e}  已拉取的部分已保存，下次运行继续")
        _append(path, buf), cache.update(buf)
    return cache


def cluster_authors(ids: Iterable[str], details: dict[str, dict]) -> dict[str, str]:
    """把同一 DBLP 消歧标识（带编号）或同一 ORCID 的 S2 作者号合并，返回 S2 作者号 → 人员号 "S2-<id>"。"""
    parent: dict[str, str] = {}

    def find(x):
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    owner: dict[str, str] = {}
    for aid in ids:
        find(aid)
        ext = (details.get(aid) or {}).get("externalIds") or {}
        keys = [f"dblp:{k}" for k in (ext.get("DBLP") or []) if DBLP_DISAMBIGUATED.search(k)]
        if ext.get("ORCID"):
            keys.append(f"orcid:{ext['ORCID']}")
        for k in keys:
            if k in owner:
                parent[find(aid)] = find(owner[k])
            else:
                owner[k] = aid
    groups = defaultdict(list)
    for aid in ids:
        groups[find(aid)].append(aid)
    out = {}
    for members in groups.values():
        # 以论文最多的作者号作为这个人的主号
        main = max(members, key=lambda a: ((details.get(a) or {}).get("paperCount") or 0, a))
        for a in members:
            out[a] = f"S2-{main}"
    return out


def unify(works: list[dict], matches: dict[str, dict], cfg: dict, allow_api: bool) -> tuple[dict, dict]:
    """改写论文的作者列表与作者号（就地修改 works）。

    返回 (people, aliases)：people = 人员号 → {S2 作者号列表, 姓名, ORCID}；
    aliases = 旧的 OpenAlex 作者号 → 新人员号（用于把旧跟进记录对到新的人）。
    """
    # 收集所有 S2 作者号
    s2_ids = set()
    for w in works:
        src = matches.get(w["id"])["authors"] if w["id"] in matches else (
            [{"authorId": (a["author"]["id"] or "")[3:]} for a in w["authorships"]] if w.get("_source") == "S2" else [])
        s2_ids.update(a.get("authorId") for a in src if a.get("authorId"))
    details = fetch_author_details(cfg, sorted(s2_ids), allow_api)
    person = cluster_authors(sorted(s2_ids), details)

    pairs = defaultdict(Counter)          # OpenAlex 作者号 → 对应到的人员号计数
    for w in works:
        if w.get("_source") == "S2":
            for a in w["authorships"]:
                sid = (a["author"]["id"] or "")[3:]
                if sid:
                    a["author"]["id"] = person.get(sid, f"S2-{sid}")
            continue
        m = matches.get(w["id"])
        if not m or not m["authors"]:
            continue
        oa = w.get("authorships") or []
        by_key = {}
        for a in oa:
            for k in name_keys((a.get("author") or {}).get("display_name")):
                by_key.setdefault(k, a)
        new, n = [], len(m["authors"])
        for i, s in enumerate(m["authors"]):
            ref = oa[i] if i < len(oa) and name_keys(s.get("name")) & name_keys(
                (oa[i].get("author") or {}).get("display_name")) else next(
                (by_key[k] for k in name_keys(s.get("name")) if k in by_key), None)
            sid = s.get("authorId")
            pid = person.get(sid, f"S2-{sid}") if sid else ((ref or {}).get("author") or {}).get("id")
            if ref and sid and (ref.get("author") or {}).get("id"):
                pairs[ref["author"]["id"]][pid] += 1
            new.append({
                "author": {"id": pid, "display_name": s.get("name") or ((ref or {}).get("author") or {}).get("display_name"),
                           "orcid": ((ref or {}).get("author") or {}).get("orcid")},
                "author_position": "first" if i == 0 else ("last" if i == n - 1 else "middle"),
                "is_corresponding": bool((ref or {}).get("is_corresponding")),
                "institutions": (ref or {}).get("institutions") or [],
            })
        w["authorships"] = new
        if m.get("citationCount"):
            w["cited_by_count"] = max(w.get("cited_by_count") or 0, m["citationCount"])

    # OpenAlex 作者号只对应到唯一一个人时，才把它在其他（S2 没匹配上的）论文里也替换掉
    aliases = {oa: next(iter(c)) for oa, c in pairs.items() if len(c) == 1}
    for w in works:
        if w["id"] in matches or w.get("_source") == "S2":
            continue
        for a in w.get("authorships") or []:
            oid = (a.get("author") or {}).get("id")
            if oid in aliases:
                a["author"]["id"] = aliases[oid]

    people = defaultdict(lambda: {"s2_ids": [], "names": Counter(), "orcid": None})
    for sid, pid in person.items():
        d = details.get(sid) or {}
        p = people[pid]
        p["s2_ids"].append(sid)
        if d.get("name"):
            p["names"][d["name"]] += (d.get("paperCount") or 0) + 1
        orcid = (d.get("externalIds") or {}).get("ORCID")
        p["orcid"] = p["orcid"] or (f"https://orcid.org/{orcid}" if orcid else None)
    # 统一显示名：取最完整（最长）且常用的写法
    for pid, p in people.items():
        p["name"] = max(p["names"], key=lambda n: (len(n), p["names"][n])) if p["names"] else None
    for w in works:
        for a in w.get("authorships") or []:
            pid = (a.get("author") or {}).get("id") or ""
            if pid.startswith("S2-") and people.get(pid, {}).get("name"):
                a["author"]["display_name"] = people[pid]["name"]
                a["author"]["orcid"] = a["author"].get("orcid") or people[pid]["orcid"]
    _short = lambda x: x.rsplit("/", 1)[-1]
    out = {_short(k): v for k, v in aliases.items()}
    # 被合并进别人的 S2 作者号也记一笔：以前按这个号记的跟进 / 收藏，会自动对到合并后的人
    out.update({f"S2-{sid}": pid for sid, pid in person.items() if f"S2-{sid}" != pid})
    return dict(people), out


def author_profiles(cfg: dict, people: dict, wanted: list[str], allow_api: bool,
                    details: dict | None = None) -> list[dict]:
    """为 S2 人员生成与 OpenAlex 作者档案同结构的履历（h 指数、总发文、学术起始年）。

    学术起始年取这个人所有作者号下论文年份的第 5 百分位，避免被个别归错的老论文拉早。
    """
    path = S2_DIR / "author_years.jsonl"
    years = _load(path)
    details = details if details is not None else _load(S2_DIR / "authors.jsonl")
    need = [sid for pid in wanted for sid in people.get(pid, {}).get("s2_ids", []) if sid not in years]
    if need and allow_api:
        print(f"[S2 履历] 拉取 {len(need)} 个作者号的论文年份（用于学术年龄）…")
        client = S2Client((cfg.get("s2") or {}).get("pause"))
        buf = {}
        try:
            for sid, a in client.batch("author", need, "papers.year", 100):
                buf[sid] = sorted(p.get("year") for p in ((a or {}).get("papers") or []) if p.get("year"))
                if len(buf) >= 2000:
                    _append(path, buf), years.update(buf), buf.clear()
        except S2Unavailable as e:
            print(f"    {e}  已拉取的部分已保存，下次运行继续")
        _append(path, buf), years.update(buf)

    out = []
    for pid in wanted:
        p = people.get(pid)
        if not p:
            continue
        ys = sorted(y for sid in p["s2_ids"] for y in years.get(sid, []))
        ds = [details.get(sid) or {} for sid in p["s2_ids"]]
        start = ys[int(0.05 * (len(ys) - 1))] if ys else None
        out.append({
            "id": f"https://www.semanticscholar.org/author/{pid}",
            "works_count": sum(d.get("paperCount") or 0 for d in ds) or None,
            "cited_by_count": sum(d.get("citationCount") or 0 for d in ds) or None,
            # 没拉到档案时 h 指数记为未知（而不是 0），以免把资深学者误判成新人
            "summary_stats": {"h_index": max((d["hIndex"] for d in ds if d.get("hIndex") is not None), default=None)},
            "affiliations": [{"institution": None, "years": [start]}] if start else [],
            "_has_years": bool(ys),
        })
    return out
