"""Explicit first-administrator bootstrap for a fresh enterprise database."""

from __future__ import annotations

import argparse
import getpass
import os

from law_agent.config import load_service_config
from law_agent.review.user_admin import ManagedUser, PostgresUserAdminStore, UserAdminStore


def bootstrap_first_admin(
    store: UserAdminStore,
    *,
    username: str,
    display_name: str,
    password: str,
) -> ManagedUser:
    if store.list_users():
        raise RuntimeError("数据库已存在用户，请登录后通过管理员接口维护账号")
    return store.create_user(username, display_name, password, "admin")


def main() -> None:
    parser = argparse.ArgumentParser(description="Create the first CrossComply administrator")
    parser.add_argument("--username", required=True)
    parser.add_argument("--display-name", required=True)
    args = parser.parse_args()
    password = os.getenv("CROSSCOMPLY_BOOTSTRAP_ADMIN_PASSWORD") or getpass.getpass(
        "Initial administrator password: "
    )
    created = bootstrap_first_admin(
        PostgresUserAdminStore(load_service_config().postgres.dsn),
        username=args.username,
        display_name=args.display_name,
        password=password,
    )
    print(f"Created administrator {created.username} ({created.id})")


if __name__ == "__main__":
    main()
