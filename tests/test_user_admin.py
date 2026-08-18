from __future__ import annotations

import pytest

from law_agent.review.user_admin import (
    DuplicateUsernameError,
    InMemoryUserAdminStore,
    InvalidRoleError,
    WeakPasswordError,
)

VALID_PASSWORD = "Correct-Horse-42"


def test_store_starts_empty_and_creates_user_without_seed_accounts() -> None:
    store = InMemoryUserAdminStore()

    assert store.list_users() == []

    user = store.create_user(
        username="Requester@Example.com ",
        display_name="采购申请人",
        password=VALID_PASSWORD,
        role="requester",
    )

    assert user.username == "requester@example.com"
    assert user.display_name == "采购申请人"
    assert user.role == "requester"
    assert user.active is True
    assert store.authenticate("REQUESTER@example.com", VALID_PASSWORD) == user


def test_password_is_hashed_and_duplicate_username_is_rejected() -> None:
    store = InMemoryUserAdminStore()
    user = store.create_user("reviewer@example.com", "审核人", VALID_PASSWORD, "reviewer")

    assert store.password_hash_for_test(user.id) != VALID_PASSWORD
    assert VALID_PASSWORD not in store.password_hash_for_test(user.id)
    with pytest.raises(DuplicateUsernameError):
        store.create_user(
            "REVIEWER@example.com",
            "另一个审核人",
            "Another-Secure-42",
            "reviewer",
        )


def test_disabled_user_cannot_authenticate_and_can_be_reenabled() -> None:
    store = InMemoryUserAdminStore()
    user = store.create_user("admin@example.com", "管理员", VALID_PASSWORD, "admin")

    disabled = store.set_active(user.id, False)

    assert disabled.active is False
    assert store.authenticate(user.username, VALID_PASSWORD) is None
    assert store.set_active(user.id, True).active is True
    assert store.authenticate(user.username, VALID_PASSWORD) is not None


def test_reset_password_invalidates_old_password() -> None:
    store = InMemoryUserAdminStore()
    user = store.create_user("reviewer@example.com", "审核人", VALID_PASSWORD, "reviewer")

    updated = store.reset_password(user.id, "Different-Secure-84")

    assert updated.updated_at >= user.updated_at
    assert store.authenticate(user.username, VALID_PASSWORD) is None
    assert store.authenticate(user.username, "Different-Secure-84") == updated


def test_assigns_each_supported_role() -> None:
    store = InMemoryUserAdminStore()
    user = store.create_user("member@example.com", "成员", VALID_PASSWORD, "requester")

    assert store.assign_role(user.id, "reviewer").role == "reviewer"
    assert store.assign_role(user.id, "admin").role == "admin"
    assert store.assign_role(user.id, "requester").role == "requester"


def test_rejects_invalid_role_and_weak_password() -> None:
    store = InMemoryUserAdminStore()

    with pytest.raises(InvalidRoleError):
        store.create_user("member@example.com", "成员", VALID_PASSWORD, "owner")  # type: ignore[arg-type]
    with pytest.raises(WeakPasswordError):
        store.create_user("member@example.com", "成员", "too-short", "requester")
