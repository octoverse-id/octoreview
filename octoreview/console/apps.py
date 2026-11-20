from django.apps import AppConfig


class ConsoleConfig(AppConfig):
    """The opt-in operator console (docs/spec.md section 13; docs/operations.md "Admin console").

    A presentation layer beside `octoreview.api`: its admin registrations read models and route
    every moderation write through the lifecycle services.
    """

    name = "octoreview.console"
    label = "console"
    verbose_name = "Operator console"

    def ready(self) -> None:
        from octoreview.console import checks  # noqa: F401  (registers the system checks)
