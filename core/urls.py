from django.urls import path

from . import activity_views, curriculum_views, note_views, views

app_name = "core"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("browse/", views.browse, name="browse"),
    path("search/", views.search, name="search"),

    # Curriculum — Administrator workspace (replaces Django admin's Trade
    # and Module changelists; see core/curriculum_views.py).
    path("curriculum/", curriculum_views.curriculum, name="curriculum"),
    path("curriculum/trades/new/", curriculum_views.trade_create, name="trade_create"),
    path("curriculum/trades/<int:pk>/edit/", curriculum_views.trade_edit, name="trade_edit"),
    path("curriculum/trades/<int:pk>/delete/", curriculum_views.trade_delete, name="trade_delete"),
    path("curriculum/modules/new/", curriculum_views.module_create, name="module_create"),
    path("curriculum/modules/<int:pk>/edit/", curriculum_views.module_edit, name="module_edit"),
    path("curriculum/modules/<int:pk>/delete/", curriculum_views.module_delete, name="module_delete"),

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

    # Activities — Trainer/Admin workspace (replaces creating these in Django Admin).
    path("activities/", activity_views.activities_manage, name="activities_manage"),
    path("activities/new/", activity_views.activity_create, name="activity_create"),
    path("activities/<int:pk>/file/", activity_views.activity_file, name="activity_file"),
    path("activities/<int:pk>/edit/", activity_views.activity_edit, name="activity_edit"),
    path("activities/<int:pk>/publish/", activity_views.activity_toggle_publish,
        name="activity_toggle_publish"),
    path("activities/<int:pk>/delete/", activity_views.activity_delete, name="activity_delete"),
]
