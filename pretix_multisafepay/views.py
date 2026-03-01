import base64
import hashlib
import hmac
import json
import logging
import requests
import urllib.parse
import uuid

from time import sleep
from decimal import Decimal
from django.contrib import messages
from django.core import signing
from django.http import Http404, HttpResponse, HttpResponseBadRequest, HttpRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.decorators import method_decorator
from django.utils.functional import cached_property
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.decorators.clickjacking import xframe_options_exempt
from django.views.decorators.csrf import csrf_exempt
from pretix.base.models import Order, OrderPayment, Quota
from pretix.base.payment import PaymentException
from pretix.base.services.locking import LockTimeoutException
from pretix.multidomain.urlreverse import build_absolute_uri, eventreverse
from requests import HTTPError
from ujson import JSONDecodeError

from .payment import MultisafepayMethod

logger = logging.getLogger(__name__)

@xframe_options_exempt
def redirect_view(request, *args, **kwargs):
    try:
        data = signing.loads(request.GET.get("data", ""), salt="safe-redirect")
    except signing.BadSignature:
        return HttpResponseBadRequest("Invalid parameter")

    if "go" in request.GET:
        if "session" in data:
            for k, v in data["session"].items():
                request.session[k] = v
        return redirect(data["url"])
    else:
        params = request.GET.copy()
        params["go"] = "1"
        r = render(
            request,
            "pretix_multisafepay/redirect.html",
            {
                "url": build_absolute_uri(
                    request.event, "plugins:pretix_multisafepay:redirect"
                )
                + "?"
                + urllib.parse.urlencode(params),
            },
        )
        r._csp_ignore = True
        return r

class MultisafepayOrderView:
    def dispatch(self, request, *args, **kwargs):
        try:
            self.order = request.event.orders.get(code=kwargs["order"])
            if (
                    hashlib.sha1(self.order.secret.lower().encode()).hexdigest()
                    != kwargs["hash"].lower()
            ):
                raise Http404("")
        except Order.DoesNotExist:
            # Do a hash comparison as well to harden timing attacks
            if (
                    "abcdefghijklmnopq".lower()
                    == hashlib.sha1("abcdefghijklmnopq".encode()).hexdigest()
            ):
                raise Http404("")
            else:
                raise Http404("")
        return super().dispatch(request, *args, **kwargs)



    @cached_property
    def payment(self):
        return get_object_or_404(
            self.order.payments,
            pk=self.kwargs["payment"],
            provider__startswith="multisafepay",
        )

    @cached_property
    def pprov(self):
        return self.payment.payment_provider

def handle_order(payment, request: HttpRequest, retry=True):
    pprov = payment.payment_provider
    data = json.loads(request.body.decode("utf-8"))

        # if data.get("status") in ("paid", "shipping", "completed") and any(
        #     line["amountRefunded"].get("value", "0.00") != "0.00"
        #     for line in data["lines"]
        # ):
        #     refundsresp = requests.get(
        #         "https://api.mollie.com/v2/orders/" + mollie_id + "/refunds?" + qp,
        #         headers=pprov.request_headers,
        #     )
        #     refundsresp.raise_for_status()
        #     refunds = refundsresp.json()["_embedded"]["refunds"]
        # else:
        #     refunds = []

    payment.info = json.dumps(data)
    payment.save()

    try:
        if (data.get("status") in ("authorized", "paid", "shipping")
            and payment.state == OrderPayment.PAYMENT_STATE_CREATED
        ):  # todo: remove paid
            payment.order.log_action("pretix_multisafepay.event." + data["status"])
            with transaction.atomic():
                # Mark order as shipped
                payment = OrderPayment.objects.select_for_update().get(pk=payment.pk)
                if payment.state != OrderPayment.PAYMENT_STATE_CREATED:
                    return  # race condition between return view and webhook

                body = {
                    # "If you leave out this parameter [lines], the entire order will be shipped."
                }

                if pprov.settings.connect_client_id and pprov.settings.access_token:
                    body["testmode"] = payment.info_data.get("mode", "live") == "test"

                payment.state = OrderPayment.PAYMENT_STATE_PENDING
                payment.save(update_fields=["state"])
            handle_order(payment, request)
        elif data.get("status") in ("paid", "completed") and payment.state in (
            OrderPayment.PAYMENT_STATE_PENDING,
            OrderPayment.PAYMENT_STATE_CREATED,
            OrderPayment.PAYMENT_STATE_CANCELED,
            OrderPayment.PAYMENT_STATE_FAILED,
        ):
            if Decimal(data["amount"]) != payment.amount:
                payment.amount = Decimal(data["amount"])
            payment.order.log_action("pretix_multisafepay.event.paid")
            payment.confirm()
        elif data.get("status") == "canceled" and payment.state in (
            OrderPayment.PAYMENT_STATE_CREATED,
            OrderPayment.PAYMENT_STATE_PENDING,
        ):
            payment.state = OrderPayment.PAYMENT_STATE_CANCELED
            payment.save()
            payment.order.log_action("pretix_multisafepay.event.canceled")
        elif (
            data.get("status") == "pending"
            and payment.state == OrderPayment.PAYMENT_STATE_CREATED
        ):
            payment.state = OrderPayment.PAYMENT_STATE_PENDING
            payment.save()
        elif data.get("status") in ("expired", "failed") and payment.state in (
            OrderPayment.PAYMENT_STATE_CREATED,
            OrderPayment.PAYMENT_STATE_PENDING,
        ):
            payment.state = OrderPayment.PAYMENT_STATE_CANCELED
            payment.save()
            payment.order.log_action("pretix_multisafepay.event." + data.get("status"))
        elif payment.state == OrderPayment.PAYMENT_STATE_CONFIRMED:
            known_refunds = [r.info_data.get("id") for r in payment.refunds.all()]
            for r in refunds:
                if r.get("status") != "failed" and r.get("id") not in known_refunds:
                    payment.create_external_refund(
                        amount=Decimal(r["amount"]["value"]), info=json.dumps(r)
                    )

        else:
            payment.order.log_action("pretix_multisafepay.event.unknown", data)

    except HTTPError:
        raise PaymentException(
            _(
                "We had trouble communicating with MultiSafepay. Please try again and get in touch "
                "with us if this problem persists."
            )
        )

@method_decorator(xframe_options_exempt, "dispatch")
class ReturnView(MultisafepayOrderView, View):
    def get(self, request, *args, **kwargs):
        # if self.payment.state not in (
        #     OrderPayment.PAYMENT_STATE_CONFIRMED,
        #     OrderPayment.PAYMENT_STATE_REFUNDED,
        #     OrderPayment.PAYMENT_STATE_CANCELED,
        # ):
        #     try:
        #         print("we got here - now capturing again")
        #         handle_order(self.payment, request, retry=True)
        #     except PaymentException as e:
        #         messages.error(self.request, str(e))
        #     except LockTimeoutException:
        #         messages.error(
        #             self.request,
        #             _(
        #                 "We received your payment but were unable to mark your ticket as "
        #                 "the server was too busy. Please check back in a couple of "
        #                 "minutes."
        #             ),
        #         )
        #     except Quota.QuotaExceededException:
        #         messages.error(
        #             self.request,
        #             _(
        #                 "We received your payment but were unable to mark your ticket as "
        #                 "paid as one of your ordered products is sold out. Please contact "
        #                 "the event organizer for further steps."
        #             ),
        #         )
        sleep(2) # Just wait, payment will show up
        return self._redirect_to_order()

    def _redirect_to_order(self):
        self.order.refresh_from_db()
        if (
            self.request.session.get("payment_multisafepay_order_secret")
            != self.order.secret
        ):
            messages.error(
                self.request,
                _(
                    "Sorry, there was an error in the payment process. Please check the link "
                    "in your emails to continue."
                ),
            )
            return redirect(eventreverse(self.request.event, "presale:event.index"))

        return redirect(
            eventreverse(
                self.request.event,
                "presale:event.order",
                kwargs={"order": self.order.code, "secret": self.order.secret},
            )
            + ("?paid=yes" if self.order.status == Order.STATUS_PAID else "")
        )
    

@method_decorator(csrf_exempt, "dispatch")
class WebhookView(View):
    def post(self, request, *args, **kwargs):
        transaction_id = request.GET.get("transactionid")
        timestamp = request.GET.get("timestamp")

        if not timestamp:
            return HttpResponse(status=403)

        try:
            if self.validate(request):
                handle_order(self.payment, request, retry=True)
                return HttpResponse("MULTISAFEPAY_OK", status=200)
            else:
                return HttpResponse(status=400)
        except LockTimeoutException:
            return HttpResponse(status=503)
        except Quota.QuotaExceededException:
            pass
        return HttpResponse(status=200)

    def validate(self, request):
        authheader = request.headers.get('Auth')
        apikey = self.payment.payment_provider.settings.get("api_key")
        payload = request.body.decode('utf-8')

        # Step 1: Base64 decode the auth header
        encoded_auth_bytes = authheader.encode("ascii")
        decoded_auth_bytes = base64.b64decode(encoded_auth_bytes)
        decoded_auth = decoded_auth_bytes.decode("ascii")

        # Step 2: Split the decoded auth header
        timestamp = decoded_auth.split(':')[0]
        signature = decoded_auth.split(':')[1]

        # Step 3: Concatenate the timestamp, colon, and payload
        concatenated_string = str(timestamp) + ":" + str(payload)

        # Step 4: SHA512 hash the concatenated string
        hashed_value = hmac.new(apikey.encode(), concatenated_string.encode(), hashlib.sha512).hexdigest()

        return hashed_value == signature

    @cached_property
    def payment(self):
        return get_object_or_404(
            OrderPayment.objects.filter(order__event=self.request.event),
            pk=self.kwargs["payment"],
            provider__startswith="multisafepay",
        )
