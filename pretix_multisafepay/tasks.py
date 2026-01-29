from django_scopes import scopes_disabled
from pretix.base.models import OrderPayment
from pretix.celery_app import app


@app.task()
@scopes_disabled()
def capture_task(payment_id, **kwargs):
    from .views import capture

    payment = OrderPayment.objects.get(pk=payment_id)
    capture(payment)
