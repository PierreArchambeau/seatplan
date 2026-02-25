from django.utils.translation import gettext_lazy as _
from pretix.base.plugins import PluginConfig

class PluginApp(PluginConfig):
    name = "pretix_simpleseatingplan"
    verbose_name = _("Simple seating plan (SVG/JSON)")

    class PretixPluginMeta:
        name = _("Simple seating plan (SVG/JSON)")
        author = "Pierre"
        description = _("Reserved seating with readable labels, category→variation mapping, holds & assignments, CSP-safe config.")
        visible = True
        version = "0.8.0"
        compatibility = "pretix>=2025.6"
        category = "FEATURE"

    def ready(self):
        from . import signals  # noqa
