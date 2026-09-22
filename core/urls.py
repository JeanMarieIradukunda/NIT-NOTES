from django.urls import path

from . import note_views, views

app_name = "core"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("browse/", views.browse, name="browse"),
    path("search/", views.search, name="search"),

    path("m/<slug:trade_key>/<slug:module_key>/", views.module_detail, name="module_detail"),
    path("m/<slug:trade_key>/<slug:module_key>/upload/", views.upload_lesson, name="upload_lesson"),

    path("l/<slug:slug>/", views.lesson_detail, name="lesson_detail"),
    path("l/<slug:slug>/pdf/", views.lesson_pdf, name="lesson_pdf"),
    path("l/<slug:slug>/activity/", views.update_activity, name="update_activity"),

    # Module notes — Trainer workspace, then the public reader.
    path("notes/", note_views.notes_manage, name="notes_manage"),
    path("notes/new/", note_views.note_create, name="note_create"),
    path("notes/<int:pk>/", note_views.note_detail, name="note_detail"),
    path("notes/<int:pk>/file/", note_views.note_file, name="note_file"),
    path("notes/<int:pk>/edit/", note_views.note_edit, name="note_edit"),
    path("notes/<int:pk>/publish/", note_views.note_toggle_publish, name="note_toggle_publish"),
    path("notes/<int:pk>/delete/", note_views.note_delete, name="note_delete"),
]
