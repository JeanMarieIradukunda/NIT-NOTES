from django.urls import path

from . import views

app_name = "ai_tools"

urlpatterns = [
    path("status/", views.status, name="status"),
    path("l/<slug:slug>/summarise/", views.summarise, name="summarise"),
    path("l/<slug:slug>/ask/", views.ask, name="ask"),
]
