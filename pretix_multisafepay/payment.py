import hashlib
import ipaddress
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
from pretix.settings import __version__ as pretix_version
from pretix.multidomain.urlreverse import build_absolute_uri
from pretix.multidomain import event_url
from pretix.helpers.http import get_client_ip
from requests import HTTPError, RequestException

from . import __version__

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
                        label=_("iDEAL | Wero"),
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

    def cancel_payment(self, payment: OrderPayment):
        if payment.state == OrderPayment.PAYMENT_STATE_PENDING and not self.abort_pending_allowed:
            try:
                headers = {
                    "accept": "application/json",
                    "content-type": "application/json"
                }
                body = {
                    "status": "cancelled",
                    "exclude_order": True
                }
                req = requests.patch(
                    "https://{env}.multisafepay.com/v1/json/orders/{order_id}?api_key={auth}".format(
                        env="api" if self.settings.get("endpoint") == "live" else "testapi",
                        auth=(self.settings.get("api_key")),
                        order_id="{}-{}-P-{}".format(
                            self.event.slug.upper(), payment.order.code, payment.local_id),
                    ),
                    timeout = 20,
                    headers = headers,
                    json = body
                )
                req.raise_for_status()
            except HTTPError:
                raise PaymentException(_(
                    "This payment is already being processed and can not be canceled any more."
                ))

        payment.state = OrderPayment.PAYMENT_STATE_CANCELED
        payment.save(update_fields=['state'])

    # def api_payment_details(self, payment: OrderPayment):
    #     return {
    #         "id": payment.info_data.get("Id"),
    #         "status": payment.info_data.get("Status"),
    #         "reference": payment.info_data.get("SixTransactionReference"),
    #         "payment_method": payment.info_data.get("PaymentMeans", {})
    #         .get("Brand", {})
    #         .get("Name"),
    #         "payment_source": payment.info_data.get("PaymentMeans", {}).get(
    #             "DisplayText"
    #         ),
    #     }

    @property
    def test_mode_message(self):
        if self.settings.endpoint == "test":
            return _(
                "The Multisafepay plugin is operating in test mode. No money will actually be transferred."
            )
        return None

    def _post(self, endpoint, *args, **kwargs):
        r = requests.post(
            "https://{env}.multisafepay.com/v1/json/orders?api_key={auth}".format(
                env="api" if self.settings.get("endpoint") == "live" else "testapi",
                auth=(self.settings.get("api_key"))
            ),
            timeout=20,
            *args,
            **kwargs,
        )
        print(r)
        return r

    def _get(self, endpoint, *args, **kwargs):
        r = requests.get(
            "https://{env}.multisafepay.com/v1/json/orders?api_key={auth}".format(
                env="api" if self.settings.get("endpoint") == "live" else "testapi",
                auth=(self.settings.get("api_key"))
            ),
            timeout=20,
            *args,
            **kwargs,
        )
        return r

    def get_locale(self, language):
        pretix_to_multisafepay_locales = {
            "en": "en_US",
            "nl": "nl_NL",
            "nl_BE": "nl_BE",
            "fr_BE": "fr_BE",
            "fr": "fr_FR",
            "de": "de_DE",
            "es": "es_ES",
            "cs": "cs_CZ",
            "pt": "pt_PT",
            "it": "it_IT",
            "nb": "nb_NO",
            "sv": "sv_SE",
            "fi": "fi_FI",
            "da": "da_DK",
            "pl": "pl_PL",
            "zh": "zh_CN",
        }
        return pretix_to_multisafepay_locales.get(
            language,
            pretix_to_multisafepay_locales.get(
                language.split("-")[0],
                pretix_to_multisafepay_locales.get(language.split("_")[0], "en_US"),
            ),
        )


    def _amount_to_decimal(self, cents):
        places = settings.CURRENCY_PLACES.get(self.event.currency, 2)
        return round_decimal(float(cents) / (10**places), self.event.currency)

    def _decimal_to_int(self, amount):
        places = settings.CURRENCY_PLACES.get(self.event.currency, 2)
        return int(amount * 10**places)

    def _get_customer_ip(request: HttpRequest):
        client_ip = get_client_ip(request)
        if not client_ip:
            return None
        try:
            client_ip = ipaddress.ip_address(client_ip)
        except ValueError:
            # Web server not set up correctly
            return None
        return client_ip

    def _get_payment_page_init_body(self, payment):
        b = {
            "RequestHeader": {
                "accept": "application/json",
                "content-type": "application/json",
            },
            "type": "redirect",
            "amount": str(self._decimal_to_int(payment.amount)),
            "currency": self.event.currency,
            "order_id": "{}-{}-P-{}".format(
                self.event.slug.upper(), payment.order.code, payment.local_id
            ),
            "description": "Order {}-{}".format(
                self.event.slug.upper(), payment.order.code
            ),
            # "PayerNote": "{}-{}".format(
            #     self.event.slug.upper(), payment.order.code
            # ),
            "gateway": self.payment_methods,
            # "Wallets": self.payment_method_wallets,
            "customer": {
                "locale": self.get_locale(payment.order.locale),
            },
            "payment_options": {
                # "notification_url": "https://melodyless-josh-interpervasively.ngrok-free.dev/org/testevent/multisafepay/webhook/" + str(payment.pk) + "/",
                "notification_url": build_absolute_uri(
                    self.event,
                    "plugins:pretix_multisafepay:webhook",
                    kwargs={
                        "payment": payment.pk,
                    }
                ),
                "notification_method": "POST",
                "redirect_url": build_absolute_uri(
                    self.event,
                    "plugins:pretix_multisafepay:return",
                    kwargs={
                        "order": payment.order.code,
                        "payment": payment.pk,
                        "hash": hashlib.sha1(
                            payment.order.secret.lower().encode(),
                        ).hexdigest(),
                    },
                ),
                "cancel_url": build_absolute_uri(
                    self.event,
                    "plugins:pretix_multisafepay:return",
                    kwargs={
                        "order": payment.order.code,
                        "payment": payment.pk,
                        "hash": hashlib.sha1(
                            payment.order.secret.lower().encode(),
                        ).hexdigest(),
                    },
                ),

            },

            "plugin": {
                "shop": "Pretix",
                "shop_version": pretix_version,
                "plugin_version": __version__,
                # "shop_root_url":
            }
        }

        mode = self.event.settings.get('payment_term_mode')

        if mode == 'days':
            b["days_active"] = self.event.settings.get('payment_term_days')
        elif mode == 'minutes':
            b["seconds_active"] = self.event.settings.get('payment_term_minutes') * 60
        else:
            b["days_active"] = "14"

        return b

    def execute_payment(self, request: HttpRequest, payment: OrderPayment):
        body = self._get_payment_page_init_body(payment)
        body["customer"]["ip_address"] = get_client_ip(request)

        print(body)  # only for debug!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!

        try:
            req = self._post(
                "v1/json/orders",
                json = body,
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
        print(data)
        payment.info = json.dumps(data)
        payment.state = OrderPayment.PAYMENT_STATE_CREATED
        payment.save()
        request.session["payment_multisafepay_order_secret"] = payment.order.secret
        return self.redirect(request, data.get("data").get("payment_url"))

    def redirect(self, request, url):
        if request.session.get("iframe_session", False):
            return (
                    build_absolute_uri(request.event, "plugins:pretix_multisafepay:redirect")
                    + "?data="
                    + signing.dumps(
                {
                    "url": url,
                    "session": {
                        "payment_multisafepay_order_secret": request.session[
                            "payment_multisafepay_order_secret"
                        ],
                    },
                },
                salt="safe-redirect",
            )

        )

        return str(url)

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
    verbose_name = _("Debit card or credit card via Multisafepay")
    public_name = _("Debit/Credit card")
    refunds_allowed = True
    cancel_flow = False
    payment_methods = "CREDITCARD"

    # @property
    # def payment_methods(self):
    #     payment_methods = []
    #     if self.settings.get("method_visa", as_type=bool):
    #         payment_methods.append("VISA")
    #     if self.settings.get("method_mastercard", as_type=bool):
    #         payment_methods.append("MASTERCARD")
    #     if self.settings.get("method_amex", as_type=bool):
    #         payment_methods.append("AMEX")
    #     return payment_methods
    #
    # @property
    # def payment_method_wallets(self):
    #     payment_methods = []
    #     if self.settings.get("method_applepay", as_type=bool):
    #         payment_methods.append("APPLEPAY")
    #     if self.settings.get("method_googlepay", as_type=bool):
    #         payment_methods.append("GOOGLEPAY")
    #     return payment_methods
    #
    # @property
    # def public_name(self) -> str:
    #     payment_methods = [gettext("Credit card")]
    #     if self.settings.get("method_applepay", as_type=bool):
    #         payment_methods.append(gettext("Apple Pay"))
    #     if self.settings.get("method_googlepay", as_type=bool):
    #         payment_methods.append(gettext("Google Pay"))
    #     return ", ".join(payment_methods)

    @property
    def is_enabled(self) -> bool:
        return self.settings.get("_enabled", as_type=bool) and self.payment_methods

class MultisafepayWero(MultisafepayMethod):
    method = "wero"
    verbose_name = _("iDeal | Wero via Multisafepay")
    public_name = _("iDeal | Wero")
    refunds_allowed = True
    cancel_flow = False
    payment_methods = "IDEAL"

class MultisafepayBancontact(MultisafepayMethod):
    method = "bancontact"
    verbose_name = _("Bancontact via Multisafepay")
    public_name = _("Bancontact")
    refunds_allowed = True
    cancel_flow = False
    payment_methods = "MISTERCASH"