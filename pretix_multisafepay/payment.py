import hashlib
import json
import logging
from decimal import Decimal

import requests
import uuid
from collections import OrderedDict
from django import forms
from django.conf import settings
from django.core import signing
from django.http import HttpRequest
from django.template.loader import get_template
from django.utils.translation import gettext_lazy as _, pgettext, gettext
from pretix.base.decimal import round_decimal
from pretix.base.models import Event, OrderPayment, OrderRefund, Order
from pretix.base.payment import BasePaymentProvider, PaymentException
from pretix.base.settings import SettingsSandbox
from pretix.multidomain.urlreverse import build_absolute_uri
from requests import HTTPError, RequestException

from . import __spec_version__

logger = logging.getLogger(__name__)


class MultisafepaySettingsHolder(BasePaymentProvider):
    identifier = "multisafepay"
    verbose_name = _("MultiSafepay")
    is_enabled = False
    is_meta = True

    def __init__(self, event: Event):
        super().__init__(event)
        self.settings = SettingsSandbox("payment", "multisafepay", event)

    @property
    def settings_form_fields(self):
        fields = [
            (
                "endpoint",
                forms.ChoiceField(
                    label=_("Endpoint"),
                    initial="live",
                    choices=(
                        ("live", pgettext("multisafepay", "Live")),
                        ("test", pgettext("multisafepay", "Testing")),
                    ),
                ),
            ),
            (
                "api_key",
                forms.CharField(
                    label=_("API key"),
                ),
            ),
            (
                "website_id",
                forms.CharField(
                    label=_("Website ID"),
                ),
            ),
        ]
        d = OrderedDict(
            fields
            + [
                (
                    "method_visa",
                    forms.BooleanField(
                        label=_("VISA"),
                        required=False,
                    ),
                ),
                (
                    "method_mastercard",
                    forms.BooleanField(
                        label=_("MasterCard"),
                        required=False,
                    ),
                ),
                (
                    "method_amex",
                    forms.BooleanField(
                        label=_("American Express"),
                        required=False,
                    ),
                ),
                (
                    "method_applepay",
                    forms.BooleanField(
                        label=_("Apple Pay"),
                        required=False,
                    ),
                ),
                (
                    "method_googlepay",
                    forms.BooleanField(
                        label=_("Google Pay"),
                        required=False,
                    ),
                ),
                (
                    "method_bancontact",
                    forms.BooleanField(
                        label=_("Bancontact"),
                        required=False,
                    ),
                ),
                # (
                #     "method_eps",
                #     forms.BooleanField(
                #         label=_("eps"),
                #         required=False,
                #     ),
                # ),

                (
                    "method_wero",
                    forms.BooleanField(
                        label=_("iDEAL | wero"),
                        required=False,
                    ),
                ),
                # (
                #     "method_paypal",
                #     forms.BooleanField(
                #         label=_("PayPal"),
                #         required=False,
                #     ),
                # ),
                # (
                #     "method_trustly",
                #     forms.BooleanField(
                #         label=_("Trustly"),
                #         required=False,
                #     ),
                # ),
                # (
                #     "method_kbc",
                #     forms.BooleanField(
                #         label=_("KBC"),
                #         required=False,
                #     ),
                # ),
                # (
                #     "method_cbc",
                #     forms.BooleanField(
                #         label=_("CBC"),
                #         required=False,
                #     ),
                # ),
                # (
                #     "method_mbway",
                #     forms.BooleanField(
                #         label=_("MB WAY"),
                #         required=False,
                #     ),
                # ),
                # (
                #     "method_wechat",
                #     forms.BooleanField(
                #         label=_("WeChat Pay"),
                #         required=False,
                #     ),
                # ),
                # (
                #     "method_dotpay",
                #     forms.BooleanField(
                #         label=_("Dotpay"),
                #         required=False,
                #     ),
                # ),
                # (
                #     "method_mybank",
                #     forms.BooleanField(
                #         label=_("MyBank"),
                #         required=False,
                #     ),
                # ),
                # (
                #     "method_alipay",
                #     forms.BooleanField(
                #         label=_("Alipay"),
                #         required=False,
                #     ),
                # ),
                # (
                #     "method_sepadebit",
                #     forms.BooleanField(
                #         label=_("SEPA Direct Debit"),
                #         required=False,
                #     ),
                # ),
                #     (
                #     "method_sofort",
                #     forms.BooleanField(
                #         label=_("SOFORT"),
                #         required=False,
                #     ),
                # )
            ]
            + list(super().settings_form_fields.items())
        )
        d.move_to_end("_enabled", last=False)
        return d


class MultisafepayMethod(BasePaymentProvider):
    method = ""
    abort_pending_allowed = False
    refunds_allowed = True
    cancel_flow = True
    payment_methods = []
    payment_method_wallets = []

    def __init__(self, event: Event):
        super().__init__(event)
        self.settings = SettingsSandbox("payment", "multisafepay", event)

    @property
    def settings_form_fields(self):
        return {}

    @property
    def identifier(self):
        return "multisafepay_{}".format(self.method)

    @property
    def is_enabled(self) -> bool:
        return self.settings.get("_enabled", as_type=bool) and self.settings.get(
            "method_{}".format(self.method), as_type=bool
        )

    def payment_refund_supported(self, payment: OrderPayment) -> bool:
        return self.refunds_allowed

    def payment_partial_refund_supported(self, payment: OrderPayment) -> bool:
        return self.refunds_allowed

    def payment_prepare(self, request, payment):
        return self.checkout_prepare(request, None)

    def payment_is_valid_session(self, request: HttpRequest):
        return True

    def payment_form_render(self, request) -> str:
        template = get_template("pretix_multisafepay/checkout_payment_form.html")
        ctx = {"request": request, "event": self.event, "settings": self.settings}
        return template.render(ctx)

    def checkout_confirm_render(self, request) -> str:
        template = get_template("pretix_multisafepay/checkout_payment_confirm.html")
        ctx = {
            "request": request,
            "event": self.event,
            "settings": self.settings,
            "provider": self,
        }
        return template.render(ctx)

    def payment_pending_render(self, request, payment) -> str:
        if payment.info:
            payment_info = json.loads(payment.info)
        else:
            payment_info = None
        template = get_template("pretix_multisafepay/pending.html")
        ctx = {
            "request": request,
            "event": self.event,
            "settings": self.settings,
            "provider": self,
            "order": payment.order,
            "payment": payment,
            "payment_info": payment_info,
        }
        return template.render(ctx)

    def payment_control_render(self, request, payment) -> str:
        if payment.info:
            payment_info = json.loads(payment.info)
            if "amount" in payment_info:
                payment_info["amount"] /= 10 ** settings.CURRENCY_PLACES.get(
                    self.event.currency, 2
                )
        else:
            payment_info = None
        template = get_template("pretix_multisafepay/control.html")
        ctx = {
            "request": request,
            "event": self.event,
            "settings": self.settings,
            "payment_info": payment_info,
            "payment": payment,
            "method": self.method,
            "provider": self,
        }
        return template.render(ctx)

    def api_payment_details(self, payment: OrderPayment):
        return {
            "id": payment.info_data.get("Id"),
            "status": payment.info_data.get("Status"),
            "reference": payment.info_data.get("SixTransactionReference"),
            "payment_method": payment.info_data.get("PaymentMeans", {})
            .get("Brand", {})
            .get("Name"),
            "payment_source": payment.info_data.get("PaymentMeans", {}).get(
                "DisplayText"
            ),
        }

    @property
    def test_mode_message(self):
        if self.settings.endpoint == "test":
            return _(
                "The Multisafepay plugin is operating in test mode. No money will actually be transferred."
            )
        return None

    def _post(self, endpoint, *args, **kwargs):
        r = requests.post(
            "https://{env}.multisafepay.com/v1/json/{ep}".format(
                env="www" if self.settings.get("endpoint") == "live" else "testapi",
                ep=endpoint,
            ),
            auth=(self.settings.get("api_user"), self.settings.get("api_pass")),
            timeout=20,
            *args,
            **kwargs,
        )
        return r

    def _get(self, endpoint, *args, **kwargs):
        r = requests.get(
            "https://{env}.multisafepay.com/v1/json/{ep}".format(
                env="api" if self.settings.get("endpoint") == "live" else "testapi",
                ep=endpoint,
            ),
            auth=(self.settings.get("api_user"), self.settings.get("api_pass")),
            timeout=20,
            *args,
            **kwargs,
        )
        return r

    def get_locale(self, language):
        multisafepay_locales = {
            "nl",
            "en",
        }

        if language[:2] in multisafepay_locales:
            return language[:2]
        return "en"

    def _amount_to_decimal(self, cents):
        places = settings.CURRENCY_PLACES.get(self.event.currency, 2)
        return round_decimal(float(cents) / (10**places), self.event.currency)

    def _decimal_to_int(self, amount):
        places = settings.CURRENCY_PLACES.get(self.event.currency, 2)
        return int(amount * 10**places)

    def _get_payment_page_init_body(self, payment):
        b = {
            "RequestHeader": {
                "SpecVersion": __spec_version__,
                "CustomerId": self.settings.customer_id,
                "RequestId": str(uuid.uuid4()),
                "RetryIndicator": 0,
                "ClientInfo": {
                    "ShopInfo": "pretix",
                },
            },
            "TerminalId": self.settings.terminal_id,
            "Payment": {
                "Amount": {
                    "Value": str(self._decimal_to_int(payment.amount)),
                    "CurrencyCode": self.event.currency,
                },
                "OrderId": "{}-{}-P-{}".format(
                    self.event.slug.upper(), payment.order.code, payment.local_id
                ),
                "Description": "Order {}-{}".format(
                    self.event.slug.upper(), payment.order.code
                ),
                "PayerNote": "{}-{}".format(
                    self.event.slug.upper(), payment.order.code
                ),
            },
            "PaymentMethods": self.payment_methods,
            "Wallets": self.payment_method_wallets,
            "Payer": {
                "LanguageCode": self.get_locale(payment.order.locale),
            },
            "ReturnUrl": {
                "Url": build_absolute_uri(
                    self.event,
                    "plugins:pretix_multisafepay:return",
                    kwargs={
                        "order": payment.order.code,
                        "payment": payment.pk,
                        "hash": hashlib.sha1(
                            payment.order.secret.lower().encode()
                        ).hexdigest(),
                    },
                ),
            },
            "Notification": {
                "SuccessNotifyUrl": build_absolute_uri(
                    self.event,
                    "plugins:pretix_multisafepay:webhook",
                    kwargs={
                        "payment": payment.pk,
                        "action": "success",
                    },
                ),
                "FailNotifyUrl": build_absolute_uri(
                    self.event,
                    "plugins:pretix_multisafepay:webhook",
                    kwargs={
                        "payment": payment.pk,
                        "action": "fail",
                    },
                ),
            },
        }
        return b

    def execute_payment(self, request: HttpRequest, payment: OrderPayment):
        try:
            req = self._post(
                "Payment/v1/PaymentPage/Initialize",
                json=self._get_payment_page_init_body(payment),
            )
            req.raise_for_status()
        except HTTPError:
            logger.exception("Multisafepay error: %s" % req.text)
            try:
                payment.info_data = req.json()
            except Exception:
                payment.info_data = {"error": True, "detail": req.text}
            payment.fail(log_data=payment.info_data)
            raise PaymentException(
                _(
                    "We had trouble communicating with MultiSafepay. Please try again and get in touch "
                    "with us if this problem persists."
                )
            )
        except RequestException as e:
            logger.exception("Multisafepay request error")
            data = {"error": True, "detail": str(e)}
            payment.fail(info=data, log_data=data)
            raise PaymentException(
                _(
                    "We had trouble communicating with MultiSafepay. Please try again and get in touch "
                    "with us if this problem persists."
                )
            )

        data = req.json()
        payment.info = json.dumps(data)
        payment.state = OrderPayment.PAYMENT_STATE_CREATED
        payment.save()
        request.session["payment_multisafepay_order_secret"] = payment.order.secret
        return self.redirect(request, data.get("RedirectUrl"))

    def shred_payment_info(self, obj: OrderPayment):
        if not obj.info:
            return
        d = json.loads(obj.info)
        if "details" in d:
            d["details"] = {k: "█" for k in d["details"].keys()}

        d["_shredded"] = True
        obj.info = json.dumps(d)
        obj.save(update_fields=["info"])


class MultisafepayCC(MultisafepayMethod):
    method = "creditcard"
    verbose_name = _("Credit card via Multisafepay")

    @property
    def payment_methods(self):
        payment_methods = []
        if self.settings.get("method_visa", as_type=bool):
            payment_methods.append("VISA")
        if self.settings.get("method_mastercard", as_type=bool):
            payment_methods.append("MASTERCARD")
        if self.settings.get("method_amex", as_type=bool):
            payment_methods.append("AMEX")
        return payment_methods

    @property
    def payment_method_wallets(self):
        payment_methods = []
        if self.settings.get("method_applepay", as_type=bool):
            payment_methods.append("APPLEPAY")
        if self.settings.get("method_googlepay", as_type=bool):
            payment_methods.append("GOOGLEPAY")
        return payment_methods

    @property
    def public_name(self) -> str:
        payment_methods = [gettext("Credit card")]
        if self.settings.get("method_applepay", as_type=bool):
            payment_methods.append(gettext("Apple Pay"))
        if self.settings.get("method_googlepay", as_type=bool):
            payment_methods.append(gettext("Google Pay"))
        return ", ".join(payment_methods)

    @property
    def is_enabled(self) -> bool:
        return self.settings.get("_enabled", as_type=bool) and self.payment_methods

class MultisafepayWero(MultisafepayMethod):
    method = "wero"
    verbose_name = _("iDeal | Wero via Multisafepay")
    public_name = _("iDeal | Wero")
    refunds_allowed = True
    cancel_flow = False
    payment_methods = ["IDEAL"]

class MultisafepayBancontact(MultisafepayMethod):
    method = "bancontact"
    verbose_name = _("Bancontact via Multisafepay")
    public_name = _("Bancontact")
    refunds_allowed = True
    cancel_flow = False
    payment_methods = ["BANCONTACT"]