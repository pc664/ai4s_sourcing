"""OpenAlex 数据抓取：论文（按检索式 + 游标翻页）与作者详情（批量）。

OpenAlex 免费、无需 API Key。文档：https://docs.openalex.org
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Iterable, Iterator

import requests

BASE = "https://api.openalex.org"
KEY_FILE = Path(__file__).resolve().parent.parent / ".openalex_key"


def resolve_api_key(config_value: str = "") -> str:
    """读取 API Key：环境变量 OPENALEX_API_KEY → 项目里的 .openalex_key 文件 → config.yaml（不推荐）。"""
    key = os.environ.get("OPENALEX_API_KEY", "").strip()
    if not key and KEY_FILE.exists():
        key = KEY_FILE.read_text(encoding="utf-8").strip()
    if not key and config_value:
        print("提示：API Key 写在 config.yaml 里不安全，建议移到 .openalex_key 文件或环境变量 OPENALEX_API_KEY")
        key = config_value.strip()
    return key

WORK_FIELDS = ",".join([
    "id", "doi", "title", "publication_year", "publication_date", "type",
    "cited_by_count", "authorships", "abstract_inverted_index",
    "primary_location", "primary_topic",
])

AUTHOR_FIELDS = ",".join([
    "id", "display_name", "orcid", "works_count", "cited_by_count",
    "summary_stats", "counts_by_year", "last_known_institutions", "affiliations",
    "topics",  # 用于判定人才类型（整个学术生涯中 AI 侧主题占比）
])


class BudgetExhausted(RuntimeError):
    """连续收到 429：今日额度用完（或请求过快）。调用方应保存进度后退出，次日续跑。"""


class OpenAlex:
    def __init__(self, api_key: str = "", mailto: str = "", pause: float = 0.12, timeout: int = 60):
        self.requests = 0
        self.s = requests.Session()
        # 2026-02-13 起 OpenAlex 强制要求 API Key（免费申请：https://openalex.org/settings/api）
        self.api_key = resolve_api_key(api_key)
        if not self.api_key:
            raise SystemExit(
                "\n缺少 OpenAlex API Key。\n"
                "1) 到 https://openalex.org/settings/api 免费注册并复制 Key\n"
                "2) 在软件文件夹里执行（把 你的Key 换成真实的 Key）：\n"
                "   echo 你的Key > .openalex_key && chmod 600 .openalex_key\n"
                "   或者设置环境变量：export OPENALEX_API_KEY=你的Key\n")
        self.mailto = mailto
        self.pause = pause
        self.timeout = timeout

    def _get(self, path: str, params: dict) -> dict:
        params = {**params, "api_key": self.api_key}
        if self.mailto:
            params["mailto"] = self.mailto
        n429 = 0
        for attempt in range(6):
            try:
                r = self.s.get(f"{BASE}{path}", params=params, timeout=self.timeout)
                if r.status_code in (401, 403):
                    raise SystemExit(
                        f"\nOpenAlex 拒绝访问（HTTP {r.status_code}）：API Key 无效，或今日额度已用完。\n"
                        f"请检查 Key 是否填对；额度次日重置。服务器返回：{r.text[:300]}\n")
                if r.status_code == 429:
                    n429 += 1
                    if n429 >= 3:
                        raise BudgetExhausted(r.text[:300])
                    raise requests.HTTPError("HTTP 429")
                if r.status_code >= 500:
                    raise requests.HTTPError(f"HTTP {r.status_code}")
                r.raise_for_status()
                self.requests += 1
                time.sleep(self.pause)
                return r.json()
            except (requests.RequestException, ValueError) as e:
                wait = 2 ** attempt
                print(f"  [重试 {attempt + 1}] {e}，{wait}s 后重试")
                time.sleep(wait)
        raise RuntimeError(f"OpenAlex 请求失败：{path} {params}")

    def iter_works(self, query: str, from_date: str, types: list[str],
                   per_page: int = 100, max_n: int = 10_000, sort: str | None = None,
                   to_date: str | None = None) -> Iterator[dict]:
        filt = [f"title_and_abstract.search:{query}",
                f"from_publication_date:{from_date}"]
        if to_date:   # 排除标成未来日期的记录，否则按发表时间倒序时会排在最前面
            filt.append(f"to_publication_date:{to_date}")
        if types:
            filt.append("type:" + "|".join(types))
        params = {"filter": ",".join(filt), "per-page": min(per_page, 100),
                  "cursor": "*", "select": WORK_FIELDS}
        if sort:
            params["sort"] = sort
        n = 0
        while True:
            data = self._get("/works", params)
            if n == 0:
                print(f"  命中 {data['meta'].get('count')} 篇（本次最多取 {max_n}）")
            for w in data.get("results", []):
                yield w
                n += 1
                if n >= max_n:
                    return
            cur = data["meta"].get("next_cursor")
            if not cur or not data.get("results"):
                return
            params["cursor"] = cur

    def count_by_year(self, query: str, from_date: str, types: list[str]) -> dict[int, int]:
        """检索式命中的论文数，按年份分组（一次请求），用来判断领域的学术热度增长。"""
        filt = [f"title_and_abstract.search:{query}", f"from_publication_date:{from_date}"]
        if types:
            filt.append("type:" + "|".join(types))
        data = self._get("/works", {"filter": ",".join(filt), "group_by": "publication_year"})
        return {int(g["key"]): g["count"] for g in data.get("group_by") or [] if str(g.get("key", "")).isdigit()}

    def get_authors(self, ids: Iterable[str], batch: int = 50) -> Iterator[dict]:
        ids = [i.rsplit("/", 1)[-1] for i in ids]
        for k in range(0, len(ids), batch):
            chunk = ids[k:k + batch]
            data = self._get("/authors", {
                "filter": "openalex:" + "|".join(chunk),
                "per-page": batch, "select": AUTHOR_FIELDS})
            yield from data.get("results", [])


def dump_jsonl(rows: Iterable[dict], path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
            n += 1
    return n


def load_jsonl(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]
