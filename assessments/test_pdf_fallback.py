"""When no PDF can be made, the evidence pack explains the real reason (to trainers only)."""

import logging
from unittest import mock

from django.urls import reverse

from . import evidence
from .models import Attempt
from .tests import Client, ExamTestBase


class DescribePdfError(ExamTestBase):
    def test_missing_native_libraries_are_named_as_that_not_as_a_missing_package(self):
        msg = evidence.describe_pdf_error(OSError("cannot load library 'libgobject-2.0-0': error 0x7e"))
        self.assertIn("WeasyPrint is installed", msg)
        self.assertIn("MSYS2", msg)
        self.assertIn("libgobject-2.0-0", msg)

    def test_a_missing_package_is_reported_as_such(self):
        msg = evidence.describe_pdf_error(ModuleNotFoundError("No module named 'weasyprint'"))
        self.assertIn("not installed", msg)
        self.assertIn("pip install weasyprint", msg)

    def test_anything_else_names_the_error_and_keeps_it_short(self):
        msg = evidence.describe_pdf_error(ValueError("boom " * 200))
        self.assertTrue(msg.startswith("the PDF engine failed (ValueError"))
        self.assertLess(len(msg), 320)


class EvidencePackFallback(ExamTestBase):
    def setUp(self):
        super().setUp()
        self.staff = Client()
        self.staff.login(username="trainer1", password="pw12345!")

    def submit_candidate(self, name, reg_no):
        c = Client()
        r = self.enter(client=c, name=name, reg_no=reg_no)
        cfg = c.get(r["Location"]).context["config"]
        self.call(c, cfg, "start")
        self.assertTrue(self.call(c, cfg, "submit").json()["submitted"])
        return Attempt.objects.get(exam=self.exam, reg_no=reg_no.upper()), c

    def test_trainer_sees_the_real_reason_and_the_print_page_still_works(self):
        self.submit_candidate("Alice Mutesi", "nit-001")
        url = reverse("assessments:exam_evidence", args=[self.exam.pk])
        with mock.patch.object(evidence, "bundle_pdf", side_effect=OSError("cannot load library 'libgobject-2.0-0'")):
            with self.assertLogs("assessments.staff_views", level=logging.WARNING) as logs:
                r = self.staff.get(url)
        self.assertEqual(r.status_code, 200)
        body = r.content.decode()
        self.assertNotIn("No PDF engine is installed", body)         # the old, misleading wording is gone
        self.assertIn("WeasyPrint is installed, but its system libraries", body)
        self.assertIn("libgobject-2.0-0", body)
        self.assertIn("Print or save as PDF", body)                  # the fallback is still offered
        self.assertIn("Each candidate starts on a new page.", body)
        self.assertIn("Alice Mutesi", body)
        self.assertTrue(any("could not be made" in line for line in logs.output))   # and it is in the log

    def test_a_genuinely_missing_package_says_so(self):
        self.submit_candidate("Alice Mutesi", "nit-001")
        url = reverse("assessments:exam_evidence", args=[self.exam.pk])
        with mock.patch.object(evidence, "bundle_pdf", side_effect=ModuleNotFoundError("No module named 'weasyprint'")), \
                self.assertLogs("assessments.staff_views", level=logging.WARNING):
            body = self.staff.get(url).content.decode()
        self.assertIn("is not installed in this environment", body)

    def test_the_reason_is_html_escaped(self):
        self.submit_candidate("Alice Mutesi", "nit-001")
        url = reverse("assessments:exam_evidence", args=[self.exam.pk])
        with mock.patch.object(evidence, "bundle_pdf", side_effect=OSError("<script>alert(1)</script>")), \
                self.assertLogs("assessments.staff_views", level=logging.WARNING):
            body = self.staff.get(url).content.decode()
        self.assertNotIn("<script>alert(1)</script>", body)

    def test_candidates_never_see_technical_details_on_their_own_sheet(self):
        a, client = self.submit_candidate("Alice Mutesi", "nit-001")
        with mock.patch.object(evidence, "render_sheet", side_effect=OSError("cannot load library 'libgobject-2.0-0'")):
            with self.assertLogs("assessments.views", level=logging.WARNING):
                r = client.get(reverse("assessments:answer_sheet", args=[a.access_key]))
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("libgobject", r.content.decode())
