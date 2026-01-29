from django.apps import AppConfig
from django.utils.translation import gettext_lazy

from . import __version__


class PluginApp(AppConfig):
    name = "pretix_multisafepay"
    verbose_name = "MultiSafepay implementation for pretix"

    class PretixPluginMeta:
        name = gettext_lazy("MultiSafepay")
        author = "Ben Carp"
        category = "PAYMENT"
        visible = True
        picture = "pretix_multisafepay/MultiSafepay-logo-color.svg"
        version = __version__
        compatibility = "pretix>=4.20.0"

        @property
        def description(self):
            t = gettext_lazy(
                "Accept payments through MultiSafepay"
            )
            t += '<div class="text text-info"><span class="fa fa-info-circle"></span> '
            return t

    def ready(self):
        from . import signals, tasks  # NOQA
