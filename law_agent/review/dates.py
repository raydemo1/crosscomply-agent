"""Calendar dates for the China and Hong Kong compliance workspace."""

from datetime import UTC, date, datetime, timedelta, timezone

BUSINESS_TIMEZONE = timezone(timedelta(hours=8), name="UTC+08:00")


def business_today() -> date:
    return datetime.now(UTC).astimezone(BUSINESS_TIMEZONE).date()
