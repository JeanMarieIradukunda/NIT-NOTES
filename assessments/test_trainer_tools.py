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


class DeleteSubmissions(BaseCase):
    """Remove one candidate's submission, or every submission of an assessment."""

    def setUp(self):
        super().setUp()
        self.exam, _ = make_exam(self.alice)
        self.other_exam, _ = make_exam(self.alice)
        self.ann = submit(self.exam, "Ann Student")
        self.ben = submit(self.exam, "Ben Student")
        self.live = Attempt.objects.create(exam=self.exam, candidate_name="Live Student", reg_no="LIVE1",
                                           status=Attempt.IN_PROGRESS)
        self.elsewhere = submit(self.other_exam, "Ann Student")
        self.clear_url = reverse("assessments:exam_clear_submissions", args=[self.exam.pk])

    def delete_url(self, attempt, exam=None):
        return reverse("assessments:attempt_delete", args=[(exam or self.exam).pk, attempt.pk])

    # ---- one candidate
    def test_deleting_one_submission_removes_only_that_candidate_and_everything_attached(self):
        from assessments.models import DeviceOpen, Violation
        Violation.objects.create(attempt=self.ann, vtype="blur")
        DeviceOpen.objects.create(attempt=self.ann, device_id="d1", opens=1)
        self.client.force_login(self.alice)
        r = self.client.post(self.delete_url(self.ann), follow=True)
        self.assertRedirects(r, reverse("assessments:exam_results", args=[self.exam.pk]))
        self.assertContains(r, "Ann Student&#x27;s submission was deleted")
        self.assertFalse(Attempt.objects.filter(pk=self.ann.pk).exists())
        self.assertEqual(Violation.objects.count(), 0)
        self.assertEqual(DeviceOpen.objects.count(), 0)
        for kept in (self.ben, self.live, self.elsewhere):                  # everyone else is untouched
            self.assertTrue(Attempt.objects.filter(pk=kept.pk).exists())
        self.assertTrue(Exam.objects.filter(pk=self.exam.pk).exists())

    def test_results_page_has_a_delete_button_per_candidate_and_a_clear_all_button(self):
        self.client.force_login(self.alice)
        html = self.client.get(reverse("assessments:exam_results", args=[self.exam.pk])).content.decode()
        for a in (self.ann, self.ben, self.live):
            self.assertIn(self.delete_url(a), html)
        self.assertNotIn(self.delete_url(self.elsewhere, self.other_exam), html)
        self.assertIn(self.clear_url, html)
        self.assertIn("Clear all submissions", html)

    def test_only_post_only_the_owner_and_only_attempts_of_this_exam(self):
        self.client.force_login(self.alice)
        self.assertEqual(self.client.get(self.delete_url(self.ann)).status_code, 405)
        self.assertEqual(self.client.post(self.delete_url(self.elsewhere, self.exam)).status_code, 404)
        self.client.force_login(self.bob)
        self.assertEqual(self.client.post(self.delete_url(self.ann)).status_code, 403)
        self.client.force_login(self.student)
        self.assertEqual(self.client.post(self.delete_url(self.ann)).status_code, 403)
        self.client.logout()
        self.assertEqual(self.client.post(self.delete_url(self.ann)).status_code, 302)
        self.assertEqual(Attempt.objects.count(), 4)

    def test_administrator_can_delete_any_submission(self):
        self.client.force_login(self.admin)
        self.client.post(self.delete_url(self.ann))
        self.assertFalse(Attempt.objects.filter(pk=self.ann.pk).exists())

    # ---- the whole class
    def test_confirmation_page_counts_what_will_go(self):
        self.client.force_login(self.alice)
        r = self.client.get(self.clear_url)
        self.assertContains(r, "This cannot be undone")
        self.assertEqual((r.context["n_submitted"], r.context["n_active"]), (2, 1))
        self.assertContains(r, 'name="include_active"')
        self.assertEqual(Attempt.objects.count(), 4)                       # a GET deletes nothing

    def test_clearing_needs_the_confirmation_box(self):
        self.client.force_login(self.alice)
        r = self.client.post(self.clear_url, {})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Tick the box to confirm")
        self.assertEqual(Attempt.objects.filter(exam=self.exam).count(), 3)

    def test_clearing_removes_submitted_attempts_and_keeps_live_ones_by_default(self):
        self.client.force_login(self.alice)
        r = self.client.post(self.clear_url, {"confirm": "yes"}, follow=True)
        self.assertRedirects(r, reverse("assessments:exam_results", args=[self.exam.pk]))
        self.assertContains(r, "Deleted 2 submissions")
        remaining = set(Attempt.objects.filter(exam=self.exam).values_list("pk", flat=True))
        self.assertEqual(remaining, {self.live.pk})
        self.assertTrue(Attempt.objects.filter(pk=self.elsewhere.pk).exists())   # other assessments untouched

    def test_ticking_the_extra_box_clears_in_progress_attempts_too(self):
        questions_before = self.exam.questions.count()
        self.client.force_login(self.alice)
        self.client.post(self.clear_url, {"confirm": "yes", "include_active": "yes"})
        self.assertEqual(Attempt.objects.filter(exam=self.exam).count(), 0)
        self.assertEqual(Attempt.objects.filter(exam=self.other_exam).count(), 1)
        self.assertTrue(Exam.objects.filter(pk=self.exam.pk).exists())
        self.assertEqual(self.exam.questions.count(), questions_before)            # questions and keys survive

    def test_nothing_to_clear_is_explained_and_has_no_form(self):
        empty, _ = make_exam(self.alice)
        self.client.force_login(self.alice)
        r = self.client.get(reverse("assessments:exam_clear_submissions", args=[empty.pk]))
        self.assertContains(r, "Nothing to clear")
        self.assertNotContains(r, 'name="confirm"')

    def test_other_trainers_students_and_visitors_cannot_clear(self):
        for who, code in ((self.bob, 403), (self.student, 403)):
            self.client.force_login(who)
            self.assertEqual(self.client.get(self.clear_url).status_code, code)
            self.assertEqual(self.client.post(self.clear_url, {"confirm": "yes"}).status_code, code)
        self.client.logout()
        self.assertEqual(self.client.post(self.clear_url, {"confirm": "yes"}).status_code, 302)
        self.assertEqual(Attempt.objects.filter(exam=self.exam).count(), 3)

    # ---- the dashboard
    def test_each_dashboard_assessment_has_submissions_and_clear_all_buttons(self):
        empty, _ = make_exam(self.alice)
        self.client.force_login(self.alice)
        r = self.client.get(reverse("core:dashboard"))
        html = r.content.decode()
        self.assertIn(reverse("assessments:exam_results", args=[self.exam.pk]), html)
        self.assertIn(self.clear_url, html)                                        # has submissions: Clear all
        self.assertNotIn(reverse("assessments:exam_clear_submissions", args=[empty.pk]), html)  # none: no button
        rows = {x["title"]: x for x in r.context["exams"]}
        mine = next(x for x in r.context["exams"] if x["clear_url"] == self.clear_url)
        self.assertEqual((mine["n_attempts"], mine["n_submitted"]), (3, 2))
        self.assertIn("2 submissions", html)
        self.assertEqual(html.count("Marking guide</a>"), 3)                       # the guide button is unchanged

    def test_administrator_dashboard_has_the_same_buttons(self):
        self.client.force_login(self.admin)
        self.assertIn(self.clear_url, self.client.get(reverse("core:dashboard")).content.decode())
