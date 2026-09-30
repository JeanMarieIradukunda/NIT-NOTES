from django.conf import settings
from django.conf.urls.static import static
from django.urls import include, path

urlpatterns = [
    # Django's built-in /admin/ site has been retired — Trades and Modules
    # are now managed at /curriculum/ (see core/curriculum_views.py), and
    # everything else that used to live only in Django admin already had
    # (or now has) an in-app home: Lessons via the per-module upload flow,
    # Module notes/Activities via /notes/ and /activities/, and Trainer/
    # Administrator accounts via /accounts/trainers/.
    path("accounts/", include("accounts.urls")),
    path("ai/", include("ai_tools.urls")),
    path("", include("core.urls")),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
