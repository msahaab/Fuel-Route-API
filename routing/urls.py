from django.urls import path

from . import views

urlpatterns = [
    path("route/", views.route_plan, name="route-plan"),
]
