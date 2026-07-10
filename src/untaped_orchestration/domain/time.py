import re
from datetime import UTC, date, datetime
from typing import Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import ConfigDict, RootModel, field_validator

UTC_TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z")
CALENDAR_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


class UtcTimestamp(RootModel[str]):
    model_config = ConfigDict(frozen=True)

    @field_validator("root")
    @classmethod
    def _validate_timestamp(cls, value: str) -> str:
        if UTC_TIMESTAMP_RE.fullmatch(value) is None:
            raise ValueError("expected UTC timestamp with exact milliseconds")
        try:
            datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")
        except ValueError as error:
            raise ValueError("expected a valid UTC timestamp") from error
        return value

    @classmethod
    def from_datetime(cls, value: datetime) -> Self:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp source must be timezone-aware")
        utc_value = value.astimezone(UTC)
        milliseconds = utc_value.microsecond // 1000
        return cls(f"{utc_value:%Y-%m-%dT%H:%M:%S}.{milliseconds:03d}Z")

    def as_datetime(self) -> datetime:
        return datetime.strptime(self.root, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)

    def __str__(self) -> str:
        return self.root


class IanaTimezone(RootModel[str]):
    model_config = ConfigDict(frozen=True)

    @field_validator("root")
    @classmethod
    def _validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ValueError, ZoneInfoNotFoundError) as error:
            raise ValueError("expected a valid IANA timezone") from error
        return value

    def as_zoneinfo(self) -> ZoneInfo:
        return ZoneInfo(self.root)

    def __str__(self) -> str:
        return self.root


class CalendarDate(RootModel[str]):
    model_config = ConfigDict(frozen=True)

    @field_validator("root")
    @classmethod
    def _validate_date(cls, value: str) -> str:
        if CALENDAR_DATE_RE.fullmatch(value) is None:
            raise ValueError("expected calendar date in YYYY-MM-DD form")
        try:
            date.fromisoformat(value)
        except ValueError as error:
            raise ValueError("expected a valid calendar date") from error
        return value

    @classmethod
    def from_date(cls, value: date) -> Self:
        return cls(value.isoformat())

    def as_date(self) -> date:
        return date.fromisoformat(self.root)

    def __str__(self) -> str:
        return self.root


def format_utc_timestamp(value: datetime) -> UtcTimestamp:
    return UtcTimestamp.from_datetime(value)


def local_calendar_date(timestamp: UtcTimestamp, timezone: IanaTimezone) -> CalendarDate:
    return CalendarDate.from_date(timestamp.as_datetime().astimezone(timezone.as_zoneinfo()).date())
