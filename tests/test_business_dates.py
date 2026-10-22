from datetime import UTC, date, datetime

import pytest

from law_agent.review import dates


@pytest.mark.parametrize(("utc_time", "expected"), [
    ("2026-10-20T15:59:00+00:00", date(2026, 10, 20)),
    ("2026-10-20T16:03:00+00:00", date(2026, 10, 21)),
    ("2026-10-21T00:03:00+00:00", date(2026, 10, 21)),
])
def test_business_date_uses_utc_plus_eight(monkeypatch, utc_time, expected):
    class Clock:
        @staticmethod
        def now(tz):
            assert tz is UTC
            return datetime.fromisoformat(utc_time)

    monkeypatch.setattr(dates, "datetime", Clock)
    assert dates.business_today() == expected
