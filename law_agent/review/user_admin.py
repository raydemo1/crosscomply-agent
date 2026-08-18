"""Administrative user lifecycle for a single-enterprise deployment.

This module deliberately has no seed-account behavior.  The first administrator
must be created by an explicit deployment/bootstrap action, after which the same
operations can be exposed through an administrator-only API.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Literal, Protocol, cast
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
from pwdlib import PasswordHash

UserRole = Literal["requester", "reviewer", "admin"]
VALID_ROLES = frozenset({"requester", "reviewer", "admin"})


class UserAdminError(ValueError):
    """Base class for user administration validation failures."""


class DuplicateUsernameError(UserAdminError):
    """Raised when a normalized username is already registered."""


class InvalidRoleError(UserAdminError):
    """Raised when a role is outside the enterprise role vocabulary."""


class WeakPasswordError(UserAdminError):
    """Raised when a password does not meet the minimum local policy."""


class UserNotFoundError(UserAdminError):
    """Raised when an administrative operation targets an unknown user."""


@dataclass(frozen=True)
class ManagedUser:
    id: str
    username: str
    display_name: str
    role: UserRole
    active: bool
    created_at: datetime
    updated_at: datetime


class UserAdminStore(Protocol):
    def create_user(
        self,
        username: str,
        display_name: str,
        password: str,
        role: UserRole,
    ) -> ManagedUser: ...

    def list_users(self) -> list[ManagedUser]: ...

    def get_user(self, user_id: str) -> ManagedUser | None: ...

    def set_active(self, user_id: str, active: bool) -> ManagedUser: ...

    def reset_password(self, user_id: str, password: str) -> ManagedUser: ...

    def assign_role(self, user_id: str, role: UserRole) -> ManagedUser: ...

    def authenticate(self, username: str, password: str) -> ManagedUser | None: ...


def _normalize_username(username: str) -> str:
    normalized = username.strip().lower()
    if not normalized:
        raise UserAdminError("username must not be empty")
    return normalized


def _validate_display_name(display_name: str) -> str:
    normalized = display_name.strip()
    if not normalized:
        raise UserAdminError("display_name must not be empty")
    return normalized


def _validate_role(role: str) -> UserRole:
    if role not in VALID_ROLES:
        raise InvalidRoleError(f"unsupported role: {role}")
    return cast(UserRole, role)


def _validate_password(password: str) -> None:
    if len(password) < 12 or password.isspace():
        raise WeakPasswordError("password must contain at least 12 characters")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _row_to_user(row: dict[str, Any]) -> ManagedUser:
    return ManagedUser(
        id=row["id"],
        username=row["username"],
        display_name=row["display_name"],
        role=_validate_role(row["role"]),
        active=row["active"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class InMemoryUserAdminStore:
    """Empty-by-default test double with production-equivalent password handling."""

    def __init__(self) -> None:
        self._hasher = PasswordHash.recommended()
        self._users: dict[str, tuple[ManagedUser, str]] = {}
        self._username_index: dict[str, str] = {}

    def create_user(
        self,
        username: str,
        display_name: str,
        password: str,
        role: UserRole,
    ) -> ManagedUser:
        normalized_username = _normalize_username(username)
        normalized_display_name = _validate_display_name(display_name)
        validated_role = _validate_role(role)
        _validate_password(password)
        if normalized_username in self._username_index:
            raise DuplicateUsernameError(normalized_username)
        now = _utc_now()
        user = ManagedUser(
            id=f"user_{uuid4().hex[:16]}",
            username=normalized_username,
            display_name=normalized_display_name,
            role=validated_role,
            active=True,
            created_at=now,
            updated_at=now,
        )
        self._users[user.id] = (user, self._hasher.hash(password))
        self._username_index[user.username] = user.id
        return user

    def list_users(self) -> list[ManagedUser]:
        return sorted(
            (record[0] for record in self._users.values()), key=lambda user: user.username
        )

    def get_user(self, user_id: str) -> ManagedUser | None:
        record = self._users.get(user_id)
        return record[0] if record else None

    def set_active(self, user_id: str, active: bool) -> ManagedUser:
        user, password_hash = self._get_record(user_id)
        updated = replace(user, active=active, updated_at=_utc_now())
        self._users[user_id] = (updated, password_hash)
        return updated

    def reset_password(self, user_id: str, password: str) -> ManagedUser:
        _validate_password(password)
        user, _ = self._get_record(user_id)
        now = _utc_now()
        updated = replace(user, updated_at=now)
        self._users[user_id] = (updated, self._hasher.hash(password))
        return updated

    def assign_role(self, user_id: str, role: UserRole) -> ManagedUser:
        validated_role = _validate_role(role)
        user, password_hash = self._get_record(user_id)
        updated = replace(user, role=validated_role, updated_at=_utc_now())
        self._users[user_id] = (updated, password_hash)
        return updated

    def authenticate(self, username: str, password: str) -> ManagedUser | None:
        user_id = self._username_index.get(_normalize_username(username))
        if user_id is None:
            return None
        user, password_hash = self._users[user_id]
        if not user.active or not self._hasher.verify(password, password_hash):
            return None
        return user

    def password_hash_for_test(self, user_id: str) -> str:
        """Expose only to verify the test double follows the hashing boundary."""

        return self._get_record(user_id)[1]

    def _get_record(self, user_id: str) -> tuple[ManagedUser, str]:
        try:
            return self._users[user_id]
        except KeyError as exc:
            raise UserNotFoundError(user_id) from exc


class PostgresUserAdminStore:
    """User administration backed by the enterprise PostgreSQL baseline."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._hasher = PasswordHash.recommended()

    def _connect(self) -> psycopg.Connection[Any]:
        return psycopg.connect(self._dsn, row_factory=dict_row)

    def create_user(
        self,
        username: str,
        display_name: str,
        password: str,
        role: UserRole,
    ) -> ManagedUser:
        normalized_username = _normalize_username(username)
        normalized_display_name = _validate_display_name(display_name)
        validated_role = _validate_role(role)
        _validate_password(password)
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO users (
                        id, username, display_name, role, password_hash, active
                    ) VALUES (%s, %s, %s, %s, %s, TRUE)
                    RETURNING id, username, display_name, role, active, created_at,
                              updated_at
                    """,
                    (
                        f"user_{uuid4().hex[:16]}",
                        normalized_username,
                        normalized_display_name,
                        validated_role,
                        self._hasher.hash(password),
                    ),
                )
                row = cursor.fetchone()
        except psycopg.errors.UniqueViolation as exc:
            raise DuplicateUsernameError(normalized_username) from exc
        assert row is not None
        return _row_to_user(row)

    def list_users(self) -> list[ManagedUser]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, username, display_name, role, active, created_at,
                       updated_at
                FROM users ORDER BY username
                """
            )
            return [_row_to_user(row) for row in cursor.fetchall()]

    def get_user(self, user_id: str) -> ManagedUser | None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, username, display_name, role, active, created_at,
                       updated_at
                FROM users WHERE id = %s
                """,
                (user_id,),
            )
            row = cursor.fetchone()
        return _row_to_user(row) if row else None

    def set_active(self, user_id: str, active: bool) -> ManagedUser:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE users SET active = %s, updated_at = now()
                WHERE id = %s
                RETURNING id, username, display_name, role, active, created_at,
                          updated_at
                """,
                (active, user_id),
            )
            row = cursor.fetchone()
            if row is not None and not active:
                cursor.execute("DELETE FROM sessions WHERE user_id = %s", (user_id,))
        if row is None:
            raise UserNotFoundError(user_id)
        return _row_to_user(row)

    def reset_password(self, user_id: str, password: str) -> ManagedUser:
        _validate_password(password)
        password_hash = self._hasher.hash(password)
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE users
                SET password_hash = %s, updated_at = now()
                WHERE id = %s
                RETURNING id, username, display_name, role, active, created_at,
                          updated_at
                """,
                (password_hash, user_id),
            )
            row = cursor.fetchone()
            if row is not None:
                cursor.execute("DELETE FROM sessions WHERE user_id = %s", (user_id,))
        if row is None:
            raise UserNotFoundError(user_id)
        return _row_to_user(row)

    def assign_role(self, user_id: str, role: UserRole) -> ManagedUser:
        validated_role = _validate_role(role)
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE users SET role = %s, updated_at = now()
                WHERE id = %s
                RETURNING id, username, display_name, role, active, created_at,
                          updated_at
                """,
                (validated_role, user_id),
            )
            row = cursor.fetchone()
            if row is not None:
                cursor.execute("DELETE FROM sessions WHERE user_id = %s", (user_id,))
        if row is None:
            raise UserNotFoundError(user_id)
        return _row_to_user(row)

    def authenticate(self, username: str, password: str) -> ManagedUser | None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, username, display_name, role, active, password_hash,
                       created_at, updated_at
                FROM users WHERE username = %s
                """,
                (_normalize_username(username),),
            )
            row = cursor.fetchone()
        if (
            row is None
            or not row["active"]
            or not self._hasher.verify(password, row["password_hash"])
        ):
            return None
        return _row_to_user(row)
