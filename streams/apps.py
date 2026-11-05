"""App configuration."""

from django.apps import AppConfig
from django.db.backends.signals import connection_created


def _configure_sqlite(sender, connection, **kwargs) -> None:
    """WAL + busy timeout so the web and worker processes can share SQLite."""
    if connection.vendor != "sqlite":
        return
    with connection.cursor() as cursor:
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("PRAGMA busy_timeout=5000;")


class StreamsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "streams"
    verbose_name = "直播推流"

    def ready(self) -> None:
        connection_created.connect(_configure_sqlite, dispatch_uid="streams.sqlite_pragmas")
