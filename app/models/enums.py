"""Enums de dominio. Se persisten como VARCHAR + CHECK (ver docs/DATA_MODEL.md §Enums)."""

from enum import StrEnum


class EventType(StrEnum):
    FROST = "frost"
    RAIN = "rain"
    HAIL = "hail"
    STRONG_WIND = "strong_wind"
    HEAT_WAVE = "heat_wave"

    @property
    def label_es(self) -> str:
        return _EVENT_LABELS_ES[self]


_EVENT_LABELS_ES: dict[EventType, str] = {
    EventType.FROST: "helada",
    EventType.RAIN: "lluvia",
    EventType.HAIL: "granizo",
    EventType.STRONG_WIND: "viento fuerte",
    EventType.HEAT_WAVE: "ola de calor",
}


class NotificationStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
    CANCELLED = "cancelled"
