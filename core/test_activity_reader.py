"""Tests for the in-page activity reader (opening activities without downloading)."""

from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from core.models import Activity, Module, Unit
from core.tests import HTML_OK, PDF_BYTES, BaseCase


class ActivityReader(BaseCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.module = Module.objects.create(trade=cls.level, key="GENCP302",
                                           code="GENCP302", name="C Programming")
        cls.topic = Unit.objects.create(module=cls.module, code="LO2", title="Loops")

    def make(self, title="Loops worksheet", kind="pdf", published=True):
        doc = (SimpleUploadedFile("w.pdf", PDF_BYTES, content_type="application/pdf")
               if kind == "pdf" else
               SimpleUploadedFile("w.html", HTML_OK, content_type="text/html"))
        return Activity.objects.create(module=self.module, topic=self.topic, title=title,
                                       document=doc, is_published=published,
                                       uploaded_by=self.alice)

    def detail(self, a):
        return self.client.get(reverse("core:activity_detail", args=[a.pk]))

    def file(self, a, query=""):
        return self.client.get(reverse("core:activity_file", args=[a.pk]) + query)

    # -- the reader page -----------------------------------------------------
    def test_anonymous_visitor_can_read_a_published_pdf_in_the_page(self):
        a = self.make()
        self.client.logout()
        r = self.detail(a)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "<iframe")
        self.assertContains(r, reverse("core:activity_file", args=[a.pk]))

    def test_html_activity_is_framed_in_a_sandbox(self):
        a = self.make(kind="html")
        self.client.logout()
        html = self.detail(a).content.decode()
        self.assertIn("?view=1", html)
        self.assertIn('sandbox="allow-scripts', html)
        self.assertNotIn("allow-same-origin", html)

    def test_reader_page_allows_only_same_origin_frames(self):
        a = self.make()
        csp = self.detail(a)["Content-Security-Policy"]
        self.assertIn("frame-src 'self'", csp)

    def test_draft_reader_is_hidden_except_to_owner_and_admin(self):
        a = self.make(published=False)
        self.client.logout()
        self.assertEqual(self.detail(a).status_code, 404)
        self.client.force_login(self.student)
        self.assertEqual(self.detail(a).status_code, 404)
        self.client.force_login(self.bob)
        self.assertEqual(self.detail(a).status_code, 404)
        self.client.force_login(self.alice)
        self.assertContains(self.detail(a), "Draft")
        self.client.force_login(self.admin)
        self.assertEqual(self.detail(a).status_code, 200)

    def test_edit_button_only_for_those_who_can_manage(self):
        a = self.make()
        edit = reverse("core:activity_edit", args=[a.pk])
        self.client.logout()
        self.assertNotContains(self.detail(a), edit)
        self.client.force_login(self.alice)
        self.assertContains(self.detail(a), edit)

    # -- the file behind the frame ---------------------------------------------
    def test_html_file_defaults_to_download_and_strict_sandbox(self):
        a = self.make(kind="html")
        r = self.file(a)
        self.assertTrue(r["Content-Disposition"].startswith("attachment"))
        self.assertEqual(r["Content-Security-Policy"], "sandbox; default-src 'none'")

    def test_html_file_with_view_flag_is_inline_but_still_sandboxed(self):
        a = self.make(kind="html")
        r = self.file(a, "?view=1")
        self.assertFalse(r.get("Content-Disposition", "").startswith("attachment"))
        csp = r["Content-Security-Policy"]
        self.assertTrue(csp.startswith("sandbox "))
        self.assertNotIn("allow-same-origin", csp)
        self.assertIn("default-src 'none'", csp)       # no network requests from the page
        self.assertEqual(r["X-Content-Type-Options"], "nosniff")

    def test_download_flag_still_forces_attachment_even_with_view(self):
        a = self.make(kind="html")
        r = self.file(a, "?view=1&download=1")
        self.assertTrue(r["Content-Disposition"].startswith("attachment"))

    def test_view_flag_does_not_expose_a_draft(self):
        a = self.make(kind="html", published=False)
        self.client.logout()
        self.assertEqual(self.file(a, "?view=1").status_code, 404)