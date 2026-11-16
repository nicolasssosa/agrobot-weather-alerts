"""Notifier por defecto: loguea el mensaje (reemplaza a WhatsApp, fuera de alcance). TODO(S6).

class LoggingNotifier:  # implementa app.notifiers.base.Notifier
    async def send(self, message: NotificationMessage) -> None
        - logger.info("notification_sent id=%s phone=%s text=%r", ...)
        - Enmascarar el teléfono en el log: dejar solo los últimos 4 dígitos (ej. "***1234").
"""
