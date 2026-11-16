"""Notifier por defecto: loguea el mensaje (reemplaza a WhatsApp, fuera de alcance)."""

import logging

from app.core.pii import mask_phone
from app.notifiers.base import NotificationMessage

logger = logging.getLogger(__name__)


class LoggingNotifier:
    """Implementación de `Notifier` que solo loguea (desarrollo y worker por defecto).

    Nunca loguea el teléfono completo: deja los últimos 4 dígitos (`***1234`).
    """

    async def send(self, message: NotificationMessage) -> None:
        """Loguea el aviso con el teléfono del destinatario enmascarado."""
        logger.info(
            "notification_sent id=%s phone=%s text=%r",
            message.notification_id,
            mask_phone(message.phone_number),
            message.text,
        )
