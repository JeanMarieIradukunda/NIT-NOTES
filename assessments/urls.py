from django.urls import path

from . import api, import_views, staff_views, views

app_name = "assessments"

urlpatterns = [
    # Candidate side (no account): /exam/<code>/ ...
    path("exam/<str:public_id>/", views.entry, name="entry"),
    path("exam/<str:public_id>/take/", views.take, name="take"),
    path("exam/result/<str:access_key>/", views.result, name="result"),
    path("exam/result/<str:access_key>/sheet/", views.answer_sheet, name="answer_sheet"),
    path("exam/api/<str:access_key>/start/", api.start, name="api_start"),
    path("exam/api/<str:access_key>/autosave/", api.autosave, name="api_autosave"),
    path("exam/api/<str:access_key>/heartbeat/", api.heartbeat, name="api_heartbeat"),
    path("exam/api/<str:access_key>/violation/", api.violation, name="api_violation"),
    path("exam/api/<str:access_key>/submit/", api.submit, name="api_submit"),
    path("exam/api/<str:access_key>/release/", api.release, name="api_release"),

    # Trainer / Administrator workspace
    path("assessments/", staff_views.exam_list, name="exam_list"),
    path("assessments/new/", staff_views.exam_create, name="exam_create"),
    path("assessments/<int:pk>/", staff_views.exam_detail, name="exam_detail"),
    path("assessments/<int:pk>/edit/", staff_views.exam_edit, name="exam_edit"),
    path("assessments/<int:pk>/open/", staff_views.exam_toggle_open, name="exam_toggle_open"),
    path("assessments/<int:pk>/delete/", staff_views.exam_delete, name="exam_delete"),
    path("assessments/<int:pk>/questions/new/", staff_views.question_create, name="question_create"),
    path("assessments/<int:pk>/questions/<int:qid>/edit/", staff_views.question_edit, name="question_edit"),
    path("assessments/<int:pk>/questions/<int:qid>/delete/", staff_views.question_delete, name="question_delete"),
    path("assessments/<int:pk>/import/", import_views.import_upload, name="import_upload"),
    path("assessments/<int:pk>/import/<int:draft_id>/", import_views.import_preview, name="import_preview"),
    path("assessments/<int:pk>/results/", staff_views.exam_results, name="exam_results"),
    path("assessments/<int:pk>/results/<int:aid>/", staff_views.attempt_detail, name="attempt_detail"),
    path("assessments/<int:pk>/results/<int:aid>/mark/", staff_views.attempt_mark, name="attempt_mark"),
    path("assessments/<int:pk>/results/<int:aid>/reset/", staff_views.attempt_reset, name="attempt_reset"),
    path("assessments/<int:pk>/results/<int:aid>/submit/", staff_views.attempt_force_submit, name="attempt_force_submit"),
]
