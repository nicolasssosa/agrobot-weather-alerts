"""Contrato del canal de notificación (WhatsApp en producción; fuera de alcance).

El dispatcher depende SOLO de este Protocol, nunca de una implementación concreta.
"""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class NotificationMessage:
    notification_id: int
    phone_number: str
    text: str


class NotifierError(Exception):
    """Fallo (posiblemente transitorio) al enviar. El dispatcher reintenta con backoff."""


class Notifier(Protocol):
    async def send(self, message: NotificationMessage) -> None:
        """Envía el mensaje. Debe lanzar NotifierError (u otra excepción) si falla."""
        ...
