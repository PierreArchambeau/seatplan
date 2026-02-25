from django.urls import re_path
from . import views

urlpatterns = [
    re_path(r"^control/event/(?P<organizer>[^/]+)/(?P<event>[^/]+)/simpleseatingplan/$", views.settings, name="settings"),
]

event_patterns = [
    re_path(r"^_simpleseatingplan/status/$", views.status, name="status"),
    re_path(r"^_simpleseatingplan/hold/$", views.hold, name="hold"),
    re_path(r"^_simpleseatingplan/release/$", views.release, name="release"),
    re_path(r"^_simpleseatingplan/config.js$", views.config_js, name="config_js"),
    re_path(r"^_simpleseatingplan/plan.svg$", views.plan_svg, name="plan_svg"),
]
