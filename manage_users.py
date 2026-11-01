"""成员账号管理（命令行）。第一次使用先建一个管理员，之后也可以在软件里的「成员管理」页操作。

    python3 manage_users.py add bill --name 比尔 --role admin     # 新建账号（会提示输入密码）
    python3 manage_users.py passwd bill                            # 重设密码
    python3 manage_users.py role amy viewer                        # 改角色：admin / editor / viewer
    python3 manage_users.py disable amy                            # 停用（enable 重新启用）
    python3 manage_users.py list                                   # 查看所有账号

角色：admin 管理员（可管理成员）· editor 编辑（可写跟进记录）· viewer 只读
"""
from __future__ import annotations

import argparse
import getpass
import sys

from src import store


def _ask_password() -> str:
    while True:
        p1 = getpass.getpass("输入密码（至少 8 位，输入时不显示）：")
        p2 = getpass.getpass("再输入一次：")
        if p1 != p2:
            print("两次输入不一致，请重试")
        elif len(p1) < 8:
            print("密码至少 8 位")
        else:
            return p1


def main():
    ap = argparse.ArgumentParser(description="AI4S 人才地图 · 成员账号管理")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add")
    a.add_argument("username")
    a.add_argument("--name", default="", help="显示名，例如中文姓名")
    a.add_argument("--role", default="editor", choices=list(store.ROLES))
    sub.add_parser("passwd").add_argument("username")
    r = sub.add_parser("role")
    r.add_argument("username")
    r.add_argument("role", choices=list(store.ROLES))
    sub.add_parser("disable").add_argument("username")
    sub.add_parser("enable").add_argument("username")
    sub.add_parser("list")
    args = ap.parse_args()

    try:
        if args.cmd == "add":
            store.create_user(args.username, args.name, args.role, _ask_password())
            print(f"已创建 {args.username}（{store.ROLES[args.role]}）")
        elif args.cmd == "passwd":
            store.set_password(args.username, _ask_password())
            print("密码已更新")
        elif args.cmd == "role":
            store.update_user(args.username, role=args.role)
            print(f"{args.username} 的角色已改为 {store.ROLES[args.role]}")
        elif args.cmd in ("disable", "enable"):
            store.update_user(args.username, active=args.cmd == "enable")
            print(f"{args.username} 已{'启用' if args.cmd == 'enable' else '停用'}")
        else:
            print(store.list_users().to_string(index=False) if store.user_count() else "还没有账号")
    except ValueError as e:
        sys.exit(f"失败：{e}")
    print(f"（数据库：{store.DB_PATH}）")


if __name__ == "__main__":
    main()
