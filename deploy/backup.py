"""备份 team.db（跟进记录 + 成员账号）到 backups/，保留最近 30 份。

用 SQLite 自带的在线备份接口，软件运行中也能安全备份。
手动执行：sudo -u ai4s /opt/ai4s_sourcing/.venv/bin/python deploy/backup.py
"""
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "team.db"
OUT = ROOT / "backups"
KEEP = 30

if not DB.exists():
    sys.exit("没有 team.db，跳过")
OUT.mkdir(exist_ok=True)
dst = OUT / f"team-{datetime.now():%Y%m%d-%H%M}.db"
src = sqlite3.connect(DB)
with sqlite3.connect(dst) as d:
    src.backup(d)
src.close()
dst.chmod(0o600)
for old in sorted(OUT.glob("team-*.db"))[:-KEEP]:
    old.unlink()
print(f"已备份到 {dst}")
