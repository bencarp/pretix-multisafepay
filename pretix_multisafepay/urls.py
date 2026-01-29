from django.urls import include, path
from pretix.multidomain import event_url

from .views import ReturnView, WebhookView, redirect_view

event_patterns = [
    path(
        "multisafepay/",
        include(
            [
                event_url(
                    r"^webhook/(?P<payment>[0-9]+)/(?P<action>[^/]+)/$",
                    WebhookView.as_view(),
                    name="webhook",
                    require_live=False,
                ),
                path("redirect/", redirect_view, name="redirect"),
                path(
                    "return/<str:order>/<str:hash>/<int:payment>",
                    ReturnView.as_view(),
                    name="return",
                ),
            ]
        ),
    ),
]
