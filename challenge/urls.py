from django.urls import path

from . import views

app_name = "challenge"

urlpatterns = [
    path("", views.gallery, name="gallery"),
    path("manage/", views.manage, name="manage"),
    path("new/", views.card_create, name="card_create"),
    path("preview/", views.card_preview, name="card_preview"),
    path("c/<int:pk>/", views.card_detail, name="card_detail"),
    path("c/<int:pk>.svg", views.card_picture, name="card_svg"),
    path("c/<int:pk>/picture/", views.card_picture, name="card_picture"),
    path("bulk/", views.card_bulk, name="card_bulk"),
    path("upload/", views.card_upload, name="card_upload"),
    path("c/<int:pk>/edit/", views.card_edit, name="card_edit"),
    path("c/<int:pk>/publish/", views.card_toggle, name="card_toggle"),
    path("c/<int:pk>/delete/", views.card_delete, name="card_delete"),
]
