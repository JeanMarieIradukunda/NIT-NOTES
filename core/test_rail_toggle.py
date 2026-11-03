"""The desktop menu column can be hidden and shown again, and the choice is remembered."""

from pathlib import Path

from django.conf import settings
from django.test import TestCase, override_settings
from django.urls import reverse

PLAIN_STATIC = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
STATIC = Path(settings.BASE_DIR) / "static"


@override_settings(STORAGES=PLAIN_STATIC)
class RailToggleTests(TestCase):
    def test_page_has_the_toggle_and_restores_the_saved_state_before_paint(self):
        html = self.client.get(reverse("accounts:login")).content.decode()
        self.assertIn('id="railToggle"', html)
        self.assertIn('aria-controls="rail"', html)
        self.assertIn('"nit-rail"', html)                      # inline restore in <head>, so no flash

    def test_script_and_css_wire_it_up(self):
        js = (STATIC / "js" / "shell.js").read_text(encoding="utf-8")
        self.assertIn("rail-hidden", js)
        self.assertIn("nit-rail", js)
        css = (STATIC / "css" / "platform.css").read_text(encoding="utf-8")
        self.assertIn("html.rail-hidden .shell", css)
