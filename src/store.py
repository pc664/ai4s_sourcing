"""团队共享存储（SQLite）：跟进记录、修改历史、成员账号。

数据库默认是项目根目录下的 team.db，可用环境变量 AI4S_DB 指定其他路径。
SQLite 开启 WAL 模式，适合一个服务器进程同时服务小组内多人读写。
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("AI4S_DB") or ROOT / "team.db")

OVERRIDABLE = ["人才类型", "层级", "主学科", "主方法"]
CRM_FIELDS = (["状态", "关系强度", "转化意向", "核实状态"]
              + [f"{c}(人工)" for c in OVERRIDABLE] + ["负责人", "备注"])
CRM_COLS = ["author_id", "姓名"] + CRM_FIELDS + ["更新人", "更新时间"]

ROLES = {"admin": "管理员", "editor": "编辑", "viewer": "只读"}
PBKDF2_ITER = 240_000
MAX_FAILS, LOCK_MINUTES = 5, 10


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


@contextmanager
def connect(path: Path | None = None):
    con = sqlite3.connect(path or DB_PATH, timeout=15)
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
        con.execute("PRAGMA busy_timeout=15000")
        _init(con)
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def _init(con: sqlite3.Connection):
    cols = ", ".join(f"{_q(c)} TEXT DEFAULT ''" for c in CRM_COLS[1:])
    con.execute(f"CREATE TABLE IF NOT EXISTS crm (author_id TEXT PRIMARY KEY, {cols}, 版本 INTEGER DEFAULT 0)")
    con.execute("""CREATE TABLE IF NOT EXISTS crm_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, author_id TEXT, 姓名 TEXT,
        字段 TEXT, 旧值 TEXT, 新值 TEXT, 修改人 TEXT, 时间 TEXT)""")
    con.execute("CREATE INDEX IF NOT EXISTS crm_log_author ON crm_log(author_id)")
    con.execute("""CREATE TABLE IF NOT EXISTS users (
        username TEXT PRIMARY KEY, display_name TEXT, role TEXT,
        pw_hash TEXT, salt TEXT, iterations INTEGER,
        active INTEGER DEFAULT 1, failed INTEGER DEFAULT 0, locked_until TEXT DEFAULT '',
        created_at TEXT, last_login TEXT DEFAULT '')""")
    con.execute("""CREATE TABLE IF NOT EXISTS favorites (
        author_id TEXT PRIMARY KEY, 姓名 TEXT, 分组 TEXT DEFAULT '', 理由 TEXT DEFAULT '',
        收藏人 TEXT, 收藏时间 TEXT)""")


# ------------------------------------------------------------------ 团队收藏
FAV_GROUPS = ["重点关注", "近期约见", "长期跟踪"]


def list_favorites(path: Path | None = None) -> pd.DataFrame:
    with connect(path) as con:
        return pd.read_sql_query("SELECT * FROM favorites ORDER BY 收藏时间 DESC", con).fillna("")


def add_favorite(author_id: str, name: str, user: str, group: str = "", reason: str = "",
                 path: Path | None = None) -> bool:
    """加入（或更新）团队收藏。返回 True 表示新加入，False 表示更新了已有收藏的分组/理由。"""
    now = _now()
    with connect(path) as con:
        old = con.execute("SELECT 分组, 理由 FROM favorites WHERE author_id=?", (author_id,)).fetchone()
        if old:
            con.execute("UPDATE favorites SET 分组=?, 理由=? WHERE author_id=?", (group, reason, author_id))
            if (old[0], old[1]) != (group, reason):
                con.execute("INSERT INTO crm_log (author_id, 姓名, 字段, 旧值, 新值, 修改人, 时间) VALUES (?,?,?,?,?,?,?)",
                            (author_id, name, "团队收藏", f"{old[0]}｜{old[1]}", f"{group}｜{reason}", user, now))
            return False
        con.execute("INSERT INTO favorites (author_id, 姓名, 分组, 理由, 收藏人, 收藏时间) VALUES (?,?,?,?,?,?)",
                    (author_id, name, group, reason, user, now))
        con.execute("INSERT INTO crm_log (author_id, 姓名, 字段, 旧值, 新值, 修改人, 时间) VALUES (?,?,?,?,?,?,?)",
                    (author_id, name, "团队收藏", "", f"加入收藏：{group}｜{reason}".rstrip("｜"), user, now))
        return True


def remove_favorite(author_id: str, user: str, path: Path | None = None) -> bool:
    with connect(path) as con:
        row = con.execute("SELECT 姓名 FROM favorites WHERE author_id=?", (author_id,)).fetchone()
        if not row:
            return False
        con.execute("DELETE FROM favorites WHERE author_id=?", (author_id,))
        con.execute("INSERT INTO crm_log (author_id, 姓名, 字段, 旧值, 新值, 修改人, 时间) VALUES (?,?,?,?,?,?,?)",
                    (author_id, row[0], "团队收藏", "已收藏", "取消收藏", user, _now()))
        return True


# ------------------------------------------------------------------ 跟进记录
def load_crm(path: Path | None = None) -> pd.DataFrame:
    with connect(path) as con:
        df = pd.read_sql_query("SELECT * FROM crm", con)
    df = df.reindex(columns=CRM_COLS + ["版本"])
    df[CRM_COLS] = df[CRM_COLS].fillna("")
    df["版本"] = df["版本"].fillna(0).astype(int)
    return df


class Conflict(Exception):
    """保存时发现别人在你打开页面之后已经改过这条记录。"""


def save_crm(row: dict, user: str, path: Path | None = None, expected_version: int | None = None) -> int:
    """写入一条跟进记录，并把有变化的字段记入修改历史。返回变化的字段数。

    expected_version：打开表单时这条记录的「版本」（新记录为 0）。数据库里的版本和它不一致，
    说明期间有人改过，抛出 Conflict，避免互相覆盖。
    """
    aid = row["author_id"]
    with connect(path) as con:
        con.commit()
        con.execute("BEGIN IMMEDIATE")      # 先拿写锁再读，保证「读版本 → 写入」之间不会被别人插队
        cur = con.execute("SELECT * FROM crm WHERE author_id=?", (aid,))
        names = [d[0] for d in cur.description]
        old = dict(zip(names, cur.fetchone() or [""] * len(names)))
        if expected_version is not None and int(old.get("版本") or 0) != int(expected_version):
            raise Conflict(f"{old.get('更新人') or '有人'} 在 {old.get('更新时间')} 改过这条记录")
        now = _now()
        changed = [(f, str(old.get(f) or ""), str(row.get(f) or "")) for f in CRM_FIELDS
                   if str(old.get(f) or "") != str(row.get(f) or "")]
        if not changed and old.get("author_id"):
            return 0
        vals = {c: str(row.get(c) or "") for c in CRM_COLS}
        vals.update({"author_id": aid, "更新人": user, "更新时间": now})
        all_cols = CRM_COLS + ["版本"]
        vals["版本"] = int(old.get("版本") or 0) + 1
        cols = ", ".join(_q(c) for c in all_cols)
        marks = ", ".join("?" for _ in all_cols)
        con.execute(f"INSERT OR REPLACE INTO crm ({cols}) VALUES ({marks})", [vals[c] for c in all_cols])
        con.executemany("INSERT INTO crm_log (author_id, 姓名, 字段, 旧值, 新值, 修改人, 时间) VALUES (?,?,?,?,?,?,?)",
                        [(aid, row.get("姓名", ""), f, a, b, user, now) for f, a, b in changed])
        return len(changed)


def crm_history(author_id: str | None = None, limit: int = 500, path: Path | None = None) -> pd.DataFrame:
    sql = "SELECT 时间, 姓名, 字段, 旧值, 新值, 修改人 FROM crm_log"
    args: tuple = ()
    if author_id:
        sql += " WHERE author_id=?"
        args = (author_id,)
    sql += " ORDER BY id DESC LIMIT ?"
    with connect(path) as con:
        return pd.read_sql_query(sql, con, params=args + (limit,))


def migrate_csv(csv_path: Path, path: Path | None = None) -> int:
    """把旧版 crm.csv 导入数据库（只在数据库里还没有跟进记录时执行），导入后改名备份。"""
    if not csv_path.exists():
        return 0
    with connect(path) as con:
        if con.execute("SELECT COUNT(*) FROM crm").fetchone()[0]:
            return 0
    old = pd.read_csv(csv_path, dtype=str).fillna("")
    n = 0
    for r in old.to_dict("records"):
        if r.get("author_id"):
            save_crm(r, user=r.get("负责人") or "（旧版导入）", path=path)
            n += 1
    csv_path.rename(csv_path.with_suffix(".csv.migrated"))
    return n


# ------------------------------------------------------------------ 成员账号
def _hash(password: str, salt: str, iterations: int) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), iterations).hex()


def _check_password(password: str):
    if len(password) < 8:
        raise ValueError("密码至少 8 位")


def user_count(path: Path | None = None) -> int:
    with connect(path) as con:
        return con.execute("SELECT COUNT(*) FROM users WHERE active=1").fetchone()[0]


def list_users(path: Path | None = None) -> pd.DataFrame:
    with connect(path) as con:
        df = pd.read_sql_query(
            "SELECT username AS 用户名, display_name AS 姓名, role AS 角色, active AS 启用, "
            "created_at AS 创建时间, last_login AS 最近登录 FROM users ORDER BY created_at", con)
    df["角色"] = df["角色"].map(ROLES).fillna(df["角色"])
    df["启用"] = df["启用"].map({1: "是", 0: "否"})
    return df


def create_user(username: str, display_name: str, role: str, password: str, path: Path | None = None):
    username = username.strip().lower()
    if not username or not username.replace("_", "").replace("-", "").replace(".", "").isalnum():
        raise ValueError("用户名只能用字母、数字、点、下划线或短横线")
    if role not in ROLES:
        raise ValueError(f"角色只能是 {list(ROLES)}")
    _check_password(password)
    salt = secrets.token_hex(16)
    with connect(path) as con:
        if con.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
            raise ValueError(f"用户 {username} 已存在")
        con.execute("INSERT INTO users (username, display_name, role, pw_hash, salt, iterations, created_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (username, display_name.strip() or username, role, _hash(password, salt, PBKDF2_ITER),
                     salt, PBKDF2_ITER, _now()))


def set_password(username: str, password: str, path: Path | None = None):
    _check_password(password)
    salt = secrets.token_hex(16)
    with connect(path) as con:
        n = con.execute("UPDATE users SET pw_hash=?, salt=?, iterations=?, failed=0, locked_until='' "
                        "WHERE username=?", (_hash(password, salt, PBKDF2_ITER), salt, PBKDF2_ITER,
                                             username.strip().lower())).rowcount
    if not n:
        raise ValueError(f"没有用户 {username}")


def update_user(username: str, role: str | None = None, active: bool | None = None,
                display_name: str | None = None, path: Path | None = None):
    username = username.strip().lower()
    with connect(path) as con:
        row = con.execute("SELECT role, active FROM users WHERE username=?", (username,)).fetchone()
        if not row:
            raise ValueError(f"没有用户 {username}")
        new_role = role or row[0]
        new_active = row[1] if active is None else int(active)
        if role and role not in ROLES:
            raise ValueError(f"角色只能是 {list(ROLES)}")
        # 不允许把最后一个启用的管理员降级或停用
        if row[0] == "admin" and row[1] and (new_role != "admin" or not new_active):
            admins = con.execute("SELECT COUNT(*) FROM users WHERE role='admin' AND active=1").fetchone()[0]
            if admins <= 1:
                raise ValueError("至少要保留一个启用的管理员")
        con.execute("UPDATE users SET role=?, active=? WHERE username=?", (new_role, new_active, username))
        if display_name:
            con.execute("UPDATE users SET display_name=? WHERE username=?", (display_name.strip(), username))


def verify(username: str, password: str, path: Path | None = None) -> tuple[dict | None, str]:
    """返回 (用户信息, 错误提示)。连续失败 MAX_FAILS 次锁定 LOCK_MINUTES 分钟。"""
    username = (username or "").strip().lower()
    with connect(path) as con:
        row = con.execute("SELECT username, display_name, role, pw_hash, salt, iterations, active, failed, "
                          "locked_until FROM users WHERE username=?", (username,)).fetchone()
        if not row or not row[6]:
            return None, "用户名或密码错误"
        if row[8] and row[8] > _now():
            return None, f"连续输错次数过多，账号已锁定到 {row[8][11:16]}"
        if hmac.compare_digest(_hash(password or "", row[4], row[5]), row[3]):
            con.execute("UPDATE users SET failed=0, locked_until='', last_login=? WHERE username=?",
                        (_now(), username))
            return {"username": row[0], "name": row[1], "role": row[2]}, ""
        failed = row[7] + 1
        lock = ((datetime.now() + timedelta(minutes=LOCK_MINUTES)).strftime("%Y-%m-%d %H:%M:%S")
                if failed >= MAX_FAILS else "")
        con.execute("UPDATE users SET failed=?, locked_until=? WHERE username=?",
                    (0 if lock else failed, lock, username))
        return None, "用户名或密码错误" + (f"，账号已锁定 {LOCK_MINUTES} 分钟" if lock else "")
