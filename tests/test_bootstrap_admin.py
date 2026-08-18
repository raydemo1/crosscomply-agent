from law_agent.review.bootstrap_admin import bootstrap_first_admin
from law_agent.review.user_admin import InMemoryUserAdminStore


def test_bootstrap_creates_only_the_first_explicit_administrator() -> None:
    store = InMemoryUserAdminStore()

    created = bootstrap_first_admin(
        store,
        username="admin@example.com",
        display_name="系统管理员",
        password="strong-password-2026",
    )

    assert created.role == "admin"
    assert created.username == "admin@example.com"

    try:
        bootstrap_first_admin(
            store,
            username="another@example.com",
            display_name="另一管理员",
            password="another-password-2026",
        )
    except RuntimeError as exc:
        assert "已存在用户" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("应拒绝第二次初始管理员引导")
