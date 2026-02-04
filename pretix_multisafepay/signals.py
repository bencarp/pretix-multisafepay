import logging
from django.dispatch import receiver
from django.utils.translation import gettext_lazy as _
from pretix.base.settings import settings_hierarkey
from pretix.base.signals import logentry_display, register_payment_providers

logger = logging.getLogger(__name__)


@receiver(register_payment_providers, dispatch_uid="payment_multisafepay")
def register_payment_provider(sender, **kwargs):
    from .payment import (
        MultisafepayBancontact, MultisafepayCC, MultisafepaySettingsHolder, MultisafepayWero
    )

    return [
        MultisafepayBancontact,
        MultisafepayCC,
        MultisafepaySettingsHolder,
        MultisafepayWero,
    ]


@receiver(signal=logentry_display, dispatch_uid="multisafepay_logentry_display")
def pretixcontrol_logentry_display(sender, logentry, **kwargs):
    if not logentry.action_type.startswith("pretix_multisafepay.event"):
        return

    plains = {
        "paid": _("Payment captured."),
        "authorized": _("Payment authorized."),
    }
    text = plains.get(logentry.action_type[22:], None)
    if text:
        return _("Multisafepay reported an event: {}").format(text)


settings_hierarkey.add_default("payment_multisafepay_method_cc", True, bool)
