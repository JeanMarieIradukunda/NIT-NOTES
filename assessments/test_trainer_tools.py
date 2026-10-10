"""Marking queue, bulk guide editor, guide download and assessment copy."""

import io
from datetime import timedelta

from django.contrib.messages import get_messages
from django.urls import reverse
from django.utils import timezone

from assessments.models import AnswerKey, Attempt, Exam
from assessments.tests import SECRET_FILL, make_exam
from core.tests import BaseCase


def submit(exam, name, *, days_ago=0, complete=False):
    return Attempt.objects.create(
        exam=exam, candidate_name=name, reg_no=name.upper().replace(" ", ""), status=Attempt.SUBMITTED,
        submitted_at=timezone.now() - timedelta(days=days_ago), marking_complete=complete)


class MarkingQueue(BaseCase):
    url = property(lambda self: reverse("assessments:marking_queue"))

    def test_lists_unmarked_submissions_oldest_first_for_my_exams_only(self):
        mine, _ = make_exam(self.alice)
        theirs, _ = make_exam(self.bob)
        submit(mine, "Newer Candidate", days_ago=1)
        submit(mine, "Older Candidate", days_ago=5)
        submit(mine, "Already Marked", days_ago=3, complete=True)
        submit(theirs, "Someone Elses")
        self.client.force_login(self.alice)
        html = self.client.get(self.url).content.decode()
        self.assertLess(html.index("Older Candidate"), html.index("Newer Candidate"))
        self.assertNotIn("Already Marked", html)
        self.assertNotIn("Someones Elses", html)
        self.assertNotIn("Someone Elses", html)
        self.assertIn("5 days", html)
        self.assertIn("2 waiting", html)

    def test_marking_button_opens_that_attempt(self):
        exam, _ = make_exam(self.alice)
        a = submit(exam, "Pat Candidate")
        self.client.force_login(self.alice)
        self.assertContains(self.client.get(self.url), reverse("assessments:attempt_detail", args=[exam.pk, a.pk]))

    def test_administrator_sees_everyones_and_empty_state_is_friendly(self):
        self.client.force_login(self.admin)
        self.assertContains(self.client.get(self.url), "All marked")
        e1, _ = make_exam(self.alice)
        e2, _ = make_exam(self.bob)
        submit(e1, "From Alice Exam"), submit(e2, "From Bob Exam")
        html = self.client.get(self.url).content.decode()
        self.assertIn("From Alice Exam", html)
        self.assertIn("From Bob Exam", html)

    def test_students_and_visitors_are_kept_out(self):
        self.client.force_login(self.student)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 302)


class EditAllGuides(BaseCase):
    def setUp(self):
        super().setUp()
        self.exam, self.qs = make_exam(self.alice)
        self.q = self.qs[("open", 1)]
        self.url = reverse("assessments:guides_edit", args=[self.exam.pk])
        self.field = f"guide_{self.q.pk}"

    def test_shows_one_box_per_written_question_with_the_current_guide(self):
        self.client.force_login(self.alice)
        r = self.client.get(self.url)
        self.assertContains(r, f'name="{self.field}"')
        self.assertContains(r, "Mentions leases and automatic addressing.")

    def test_saving_updates_the_guide_and_keeps_the_rest_of_the_key(self):
        AnswerKey.objects.filter(question=self.q).update(data={"guide": "old", "extra": 1})
        self.client.force_login(self.alice)
        r = self.client.post(self.url, {self.field: "  New guide text  "})
        self.assertRedirects(r, reverse("assessments:exam_guide", args=[self.exam.pk]))
        self.assertEqual(AnswerKey.objects.get(question=self.q).data, {"guide": "New guide text", "extra": 1})

    def test_a_question_without_an_answer_key_gets_one(self):
        AnswerKey.objects.filter(question=self.q).delete()
        self.client.force_login(self.alice)
        self.client.post(self.url, {self.field: "Fresh guide"})
        self.assertEqual(AnswerKey.objects.get(question=self.q).data["guide"], "Fresh guide")

    def test_unchanged_text_saves_nothing(self):
        self.client.force_login(self.alice)
        current = AnswerKey.objects.get(question=self.q).data["guide"]
        r = self.client.post(self.url, {self.field: current}, follow=True)
        self.assertContains(r, "No changes to save")

    def test_too_long_guides_are_rejected_and_nothing_is_saved(self):
        self.client.force_login(self.alice)
        before = AnswerKey.objects.get(question=self.q).data
        r = self.client.post(self.url, {self.field: "x" * 5001})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "at most 5000 characters")
        self.assertEqual(AnswerKey.objects.get(question=self.q).data, before)

    def test_other_trainers_cannot_edit(self):
        self.client.force_login(self.bob)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.assertEqual(self.client.post(self.url, {self.field: "hijack"}).status_code, 403)
        self.assertNotEqual(AnswerKey.objects.get(question=self.q).data["guide"], "hijack")


class GuideDownload(BaseCase):
    def setUp(self):
        super().setUp()
        self.exam, self.qs = make_exam(self.alice)

    def url(self, fmt, pk=None):
        return reverse("assessments:guide_download", args=[pk or self.exam.pk, fmt])

    def test_word_file_contains_every_kind_of_answer(self):
        from docx import Document
        self.client.force_login(self.alice)
        r = self.client.get(self.url("docx"))
        self.assertEqual(r.status_code, 200)
        self.assertIn("wordprocessingml", r["Content-Type"])
        self.assertIn("attachment", r["Content-Disposition"])
        text = "\n".join(p.text for p in Document(io.BytesIO(r.content)).paragraphs)
        self.assertIn("✓ correct", text)
        self.assertIn(SECRET_FILL, text)
        self.assertIn("Connects networks", text)
        self.assertIn("Mentions leases and automatic addressing.", text)

    def test_pdf_download(self):
        try:
            from weasyprint import HTML
            HTML(string="<p>x</p>").write_pdf()
        except Exception:
            self.skipTest("WeasyPrint's system libraries are not installed here")
        self.client.force_login(self.alice)
        r = self.client.get(self.url("pdf"))
        self.assertEqual(r["Content-Type"], "application/pdf")
        self.assertTrue(r.content.startswith(b"%PDF"))

    def test_unknown_format_and_other_trainers(self):
        self.client.force_login(self.alice)
        self.assertEqual(self.client.get(self.url("exe")).status_code, 404)
        self.client.force_login(self.bob)
        self.assertEqual(self.client.get(self.url("docx")).status_code, 403)
        self.client.logout()
        self.assertEqual(self.client.get(self.url("docx")).status_code, 302)


class CopyAssessment(BaseCase):
    def setUp(self):
        super().setUp()
        self.exam, self.qs = make_exam(self.alice, is_open=True)
        self.url = reverse("assessments:exam_duplicate", args=[self.exam.pk])

    def test_copy_is_closed_has_new_password_and_all_questions_and_keys(self):
        submit(self.exam, "Original Candidate")
        self.client.force_login(self.alice)
        r = self.client.post(self.url)
        copy = Exam.objects.exclude(pk=self.exam.pk).get()
        self.assertRedirects(r, reverse("assessments:exam_detail", args=[copy.pk]))
        self.assertEqual(copy.title, f"Copy of {self.exam.title}")
        self.assertFalse(copy.is_open)
        self.assertTrue(copy.has_password)
        self.assertNotEqual(copy.public_id, self.exam.public_id)
        self.assertEqual(copy.created_by, self.alice)
        self.assertEqual(copy.questions.count(), self.exam.questions.count())
        self.assertEqual(copy.total_marks, self.exam.total_marks)
        self.assertEqual(copy.attempts.count(), 0)                       # candidates are not copied
        guide = copy.questions.get(section="open").key.data["guide"]
        self.assertEqual(guide, "Mentions leases and automatic addressing.")
        # the originals are untouched, and the keys are independent rows
        self.assertEqual(self.exam.questions.count(), copy.questions.count())
        self.assertFalse(set(self.exam.questions.values_list("pk", flat=True))
                         & set(copy.questions.values_list("pk", flat=True)))

    def test_new_password_is_shown_once(self):
        self.client.force_login(self.alice)
        r = self.client.post(self.url, follow=True)
        self.assertContains(r, "New exam password (shown once")

    def test_only_post_and_only_the_owner(self):
        self.client.force_login(self.alice)
        self.assertEqual(self.client.get(self.url).status_code, 405)
        self.client.force_login(self.bob)
        self.assertEqual(self.client.post(self.url).status_code, 403)
        self.assertEqual(Exam.objects.count(), 1)

    def test_administrator_can_copy_and_becomes_the_owner(self):
        self.client.force_login(self.admin)
        self.client.post(self.url)
        self.assertEqual(Exam.objects.exclude(pk=self.exam.pk).get().created_by, self.admin)

    def test_buttons_are_on_the_pages(self):
        self.client.force_login(self.alice)
        self.assertContains(self.client.get(reverse("assessments:exam_detail", args=[self.exam.pk])), self.url)
        g = self.client.get(reverse("assessments:exam_guide", args=[self.exam.pk]))
        for name in ("guides_edit",):
            self.assertContains(g, reverse(f"assessments:{name}", args=[self.exam.pk]))
        self.assertContains(g, self.exam.pk and reverse("assessments:guide_download", args=[self.exam.pk, "docx"]))
        self.assertContains(g, reverse("assessments:guide_download", args=[self.exam.pk, "pdf"]))
