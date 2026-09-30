"""Tests for the dark student dashboard (landing page)."""

from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from core.models import Activity, Module, ModuleNote, Unit
from core.tests import PDF_BYTES, BaseCase


class DashboardPage(BaseCase):
    def get(self):
        return self.client.get(reverse("core:dashboard"))

    def test_uses_the_night_theme_and_its_assets(self):
        html = self.get().content.decode()
        self.assertIn('<body class="night">', html)
        self.assertIn("css/dashboard.css", html)
        self.assertIn("css/platform.css", html)
        self.assertIn("newsreader-latin-wght-normal.woff2", html)
        self.assertIn('data-bs-theme="light"', html)          # Bootstrap mode unchanged
        self.assertNotIn("fonts.googleapis", html)            # fonts stay self-hosted
        self.assertNotIn("cdn.jsdelivr", html)

    def test_other_pages_are_not_themed(self):
        for name in ("core:browse", "core:search"):
            html = self.client.get(reverse(name)).content.decode()
            self.assertNotIn("night", html.split("<body", 1)[1].split(">", 1)[0], name)
            self.assertNotIn("dashboard.css", html, name)

    def test_empty_library_shows_guidance_for_students_and_trainers(self):
        self.client.logout()
        self.assertContains(self.get(), "Trainers are still adding notes")
        self.assertContains(self.get(), "Levels will appear here")
        self.client.force_login(self.alice)
        r = self.get()
        self.assertContains(r, "Add your first module notes")
        self.assertContains(r, reverse("core:note_create"))

    def test_newest_published_note_is_the_spotlight_and_rest_are_listed(self):
        first = self.make_note()
        self.post_note(self.alice, title="Second published note",
                       module_data={"module": first.module_id})
        second = ModuleNote.objects.latest("pk")
        self.assertNotEqual(first.pk, second.pk)
        self.client.logout()
        html = self.get().content.decode()
        self.assertIn("Latest from your trainers", html)
        # Spotlight holds the newest note, the list below holds the others.
        spot = html.split('class="nd-spot"', 1)[1].split("</a>", 1)[0]
        self.assertIn(second.title, spot)
        self.assertNotIn(second.title, html.split("Just published", 1)[1])
        self.assertIn(first.title, html.split("Just published", 1)[1])

    def test_single_note_has_no_duplicate_feed_and_levels_span_the_page(self):
        self.make_note()
        self.client.logout()
        html = self.get().content.decode()
        self.assertNotIn('id="whats-new-title"', html)
        self.assertIn("nd-body--solo", html)
        self.assertIn("Choose your level", html)

    def test_drafts_never_appear_for_students(self):
        self.make_note(published=False)
        self.client.logout()
        r = self.get()
        self.assertNotContains(r, "Loops notes")
        self.assertContains(r, "Trainers are still adding notes")

    def test_trainer_workspace_is_hidden_from_students(self):
        self.make_note()
        self.client.logout()
        self.assertNotContains(self.get(), "Your trainer workspace")
        self.client.force_login(self.alice)
        r = self.get()
        self.assertContains(r, "Your trainer workspace")
        self.assertContains(r, "Add module notes")
        self.client.force_login(self.admin)
        self.assertContains(self.get(), "Notes workspace")


class DashboardActivities(BaseCase):
    """Published activities appear on the dashboard for everyone; drafts never do."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.module = Module.objects.create(trade=cls.level, key="GENCP302",
                                           code="GENCP302", name="C Programming")
        cls.topic = Unit.objects.create(module=cls.module, code="LO2", title="Loops")

    def make_activity(self, title, published=True, name="worksheet.pdf"):
        return Activity.objects.create(
            module=self.module, topic=self.topic, title=title, is_published=published,
            uploaded_by=self.alice,
            document=SimpleUploadedFile(name, PDF_BYTES, content_type="application/pdf"))

    def get(self):
        return self.client.get(reverse("core:dashboard"))

    def test_published_activity_is_listed_for_anonymous_visitors(self):
        activity = self.make_activity("Loops worksheet")
        self.client.logout()
        r = self.get()
        self.assertContains(r, 'id="activities-title"')
        self.assertContains(r, "Loops worksheet")
        self.assertContains(r, reverse("core:activity_file", args=[activity.pk]))

    def test_published_activity_is_listed_for_signed_in_students(self):
        self.make_activity("Loops worksheet")
        self.client.force_login(self.student)
        self.assertContains(self.get(), "Loops worksheet")

    def test_draft_activities_never_appear(self):
        self.make_activity("Secret draft activity", published=False)
        self.client.logout()
        r = self.get()
        self.assertNotContains(r, "Secret draft activity")
        self.assertNotContains(r, 'id="activities-title"')

    def test_html_activity_is_offered_as_a_download(self):
        self.make_activity("Loops page", name="page.html")
        Activity.objects.filter(title="Loops page").update(document_type=Activity.TYPE_HTML)
        self.assertContains(self.get(), "Download")