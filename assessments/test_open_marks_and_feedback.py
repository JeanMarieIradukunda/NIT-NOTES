"""Marks set on each open question, and trainer comments reaching the candidate once they are marked."""

import unittest
from decimal import Decimal

from django.template.loader import render_to_string
from django.test import Client
from django.urls import reverse

from . import evidence
from .forms import QuestionForm
from .marking import candidate_feedback, feedback_visible, question_max_marks, right_id
from .models import AnswerKey, Question, allocate_open_marks
from .tests import ExamTestBase, _pdf_engine


class OpenMarksTests(ExamTestBase):
    """make_exam: MCQ 4, fill 2, match 2 and open 4 marks; one open question (weight 1)."""

    def setUp(self):
        super().setUp()
        self.staff = Client()
        self.staff.login(username="trainer1", password="pw12345!")
        self.open1 = self.qs[("open", 1)]

    def add_open(self, order, marks=None, weight=1, text=None):
        q = Question.objects.create(exam=self.exam, section="open", order=order, weight=weight, marks=marks,
                                    text=text or f"Open question {order}")
        AnswerKey.objects.create(question=q, data={"guide": f"guide {order}"})
        return q

    def maxima(self):
        return question_max_marks(self.exam, list(self.exam.questions.all()))

    # -- the maths ---------------------------------------------------------------------------------

    def test_without_custom_marks_nothing_changes(self):
        self.add_open(2)                                                    # two open questions, 4 marks, weight 1 each
        m = self.maxima()
        self.assertEqual(m[self.open1.id], Decimal("2.00"))
        self.assertEqual(self.exam.section_marks("open"), Decimal("4"))
        self.assertEqual(self.exam.total_marks, Decimal("12"))

    def test_custom_marks_are_taken_off_the_section_first(self):
        self.exam.marks_open = Decimal("10")
        self.exam.save()
        b = self.add_open(2, marks=Decimal("5"))
        c = self.add_open(3, marks=Decimal("1.5"))
        d = self.add_open(4, weight=2)                                      # shares the rest with open1 by weight
        m = self.maxima()
        self.assertEqual((m[b.id], m[c.id]), (Decimal("5.00"), Decimal("1.50")))
        self.assertEqual(m[self.open1.id], Decimal("1.17"))                 # 3.5 * 1/3
        self.assertEqual(m[d.id], Decimal("2.33"))                          # 3.5 * 2/3
        self.assertEqual(self.exam.section_marks("open"), Decimal("10.00"))
        self.assertEqual(self.exam.total_marks, Decimal("18.00"))

    def test_when_every_open_question_has_its_own_marks_the_section_total_is_their_sum(self):
        self.open1.marks = Decimal("2")
        self.open1.save()
        self.add_open(2, marks=Decimal("6.5"))
        for pool in (Decimal("0"), Decimal("4"), Decimal("20")):
            self.exam.marks_open = pool
            self.assertEqual(self.exam.section_marks("open"), Decimal("8.50"))
        self.assertEqual(self.exam.total_marks, Decimal("16.50"))

    def test_custom_marks_above_the_section_total_leave_the_others_with_zero_and_warn(self):
        b = self.add_open(2, marks=Decimal("5"))                            # pool is 4
        m = self.maxima()
        self.assertEqual((m[self.open1.id], m[b.id]), (Decimal("0.00"), Decimal("5.00")))
        self.assertTrue(self.exam.open_pool_overspent)
        page = self.staff.get(reverse("assessments:exam_detail", args=[self.exam.pk]))
        self.assertContains(page, "add up to more than")

    def test_allocate_open_marks_is_pure(self):
        out, total = allocate_open_marks(6, [(1, None, 1), (2, Decimal("2"), 1), (3, None, 3)])
        self.assertEqual(out, {1: Decimal("1.00"), 2: Decimal("2.00"), 3: Decimal("3.00")})
        self.assertEqual(total, Decimal("6.00"))

    # -- setting them -------------------------------------------------------------------------------

    def post_question(self, **fields):
        data = {"section": "open", "text": "Describe subnetting.", "weight": 1, "order": 9, **fields}
        return self.staff.post(reverse("assessments:question_create", args=[self.exam.pk]), data)

    def test_the_question_form_stores_exact_marks_for_open_questions_only(self):
        self.assertEqual(self.post_question(marks="2.5").status_code, 302)
        q = Question.objects.get(exam=self.exam, text="Describe subnetting.")
        self.assertEqual(q.marks, Decimal("2.50"))
        self.assertEqual(QuestionForm.initial_for(q)["marks"], Decimal("2.50"))
        self.post_question(section="fill", text="Blank ____", fill_accepted="x", marks="3")
        self.assertIsNone(Question.objects.get(exam=self.exam, text="Blank ____").marks)
        self.post_question(text="No own marks", marks="")
        self.assertIsNone(Question.objects.get(exam=self.exam, text="No own marks").marks)
        self.assertEqual(self.post_question(text="Negative", marks="-1").status_code, 200)     # form error, not saved
        self.assertFalse(Question.objects.filter(text="Negative").exists())

    def test_the_question_page_offers_the_marks_box(self):
        page = self.staff.get(reverse("assessments:question_create", args=[self.exam.pk]))
        self.assertContains(page, 'name="marks"')

    def test_duplicating_an_assessment_keeps_each_questions_marks(self):
        self.add_open(2, marks=Decimal("3"))
        self.staff.post(reverse("assessments:exam_duplicate", args=[self.exam.pk]))
        copy = type(self.exam).objects.exclude(pk=self.exam.pk).get()
        self.assertEqual(sorted(str(m) for m in copy.questions.filter(section="open").values_list("marks", flat=True) if m),
                         ["3.00"])

    # -- marking against them -----------------------------------------------------------------------

    def submit_attempt(self):
        _, cfg = self.open_page()
        self.call(self.client_a, cfg, "start")
        m = self.qs[("match", 1)]
        answers = {
            str(self.qs[("mcq", 1)].id): 1,
            str(self.qs[("mcq", 2)].id): 0,
            str(m.id): {"0": right_id(m.id, 0), "1": right_id(m.id, 2)},
        }
        for q in self.exam.questions.filter(section="open"):
            answers[str(q.id)] = f"My answer to {q.text}"
        self.assertTrue(self.call(self.client_a, cfg, "submit", answers=answers).json()["submitted"])
        return self.attempt()

    def mark(self, a, **fields):
        return self.staff.post(reverse("assessments:attempt_mark", args=[self.exam.pk, a.pk]), fields)

    def test_marking_is_limited_to_each_questions_own_maximum(self):
        self.exam.marks_open = Decimal("10")
        self.exam.save()
        b = self.add_open(2, marks=Decimal("5"))
        a = self.submit_attempt()
        self.mark(a, **{f"mark_{b.id}": "5.5"})                              # over its own 5
        self.assertEqual(self.attempt().open_marks, {})
        self.mark(a, **{f"mark_{b.id}": "5", f"mark_{self.open1.id}": "4"})  # open1 may use the 5 left (10 - 5)
        self.assertEqual({k: Decimal(v) for k, v in self.attempt().open_marks.items()},
                         {str(b.id): Decimal("5"), str(self.open1.id): Decimal("4")})
        self.mark(a, **{f"mark_{b.id}": "5", f"mark_{self.open1.id}": "5.01"})
        self.assertEqual(Decimal(self.attempt().open_marks[str(self.open1.id)]), Decimal("4"))   # unchanged

    def test_the_final_score_uses_the_custom_total(self):
        self.exam.marks_open = Decimal("10")
        self.exam.save()
        b = self.add_open(2, marks=Decimal("5"))
        a = self.submit_attempt()
        self.mark(a, **{f"mark_{b.id}": "4.5", f"mark_{self.open1.id}": "3"})
        a = self.attempt()
        self.assertTrue(a.marking_complete)
        self.assertEqual(a.final_score, Decimal("2") + Decimal("0") + Decimal("1") + Decimal("7.5"))  # 3 objective + 7.5 open
        shown = self.client_a.get(reverse("assessments:result", args=[a.access_key]))
        self.assertContains(shown, "10.50")
        self.assertContains(shown, "/ 18")                                   # 4 + 2 + 10 + 2


class FeedbackTests(ExamTestBase):
    """Trainer comments on each question reach the candidate once they are marked."""

    def setUp(self):
        super().setUp()
        self.staff = Client()
        self.staff.login(username="trainer1", password="pw12345!")
        _, cfg = self.open_page()
        self.call(self.client_a, cfg, "start")
        answers = {str(self.qs[("open", 1)].id): "Hands out IP addresses.", str(self.qs[("mcq", 1)].id): 1}
        self.call(self.client_a, cfg, "submit", answers=answers)
        self.q_open, self.q_mcq = self.qs[("open", 1)], self.qs[("mcq", 1)]
        self.result_url = reverse("assessments:result", args=[self.attempt().access_key])

    def mark(self, **fields):
        data = {"trainer_comment": "PRIVATE overall note", **fields}
        return self.staff.post(reverse("assessments:attempt_mark", args=[self.exam.pk, self.attempt().pk]), data)

    def test_comments_are_hidden_until_every_open_question_is_marked(self):
        self.mark(**{f"comment_{self.q_mcq.id}": "Well spotted."})           # open question not marked yet
        self.assertFalse(self.attempt().marking_complete)
        self.assertFalse(feedback_visible(self.exam, self.attempt()))
        self.assertNotContains(self.client_a.get(self.result_url), "Well spotted.")
        self.mark(**{f"comment_{self.q_mcq.id}": "Well spotted.", f"mark_{self.q_open.id}": "3"})
        self.assertContains(self.client_a.get(self.result_url), "Well spotted.")

    def test_comments_show_without_releasing_the_answers_and_never_the_overall_note(self):
        self.assertFalse(self.exam.show_answers)
        self.mark(**{f"comment_{self.q_open.id}": "Mention lease times next time.", f"mark_{self.q_open.id}": "3"})
        page = self.client_a.get(self.result_url).content.decode()
        self.assertIn("Feedback from your trainer", page)
        self.assertIn("Mention lease times next time.", page)
        self.assertNotIn("PRIVATE overall note", page)
        self.assertNotContains(self.client_a.get(reverse("assessments:answer_review", args=[self.attempt().access_key])),
                               "Mention lease", status_code=403)             # the key itself stays locked

    def test_results_that_are_not_released_keep_the_comments_back_too(self):
        self.exam.show_results = False
        self.exam.save()
        self.mark(**{f"comment_{self.q_open.id}": "Good depth.", f"mark_{self.q_open.id}": "4"})
        self.assertNotContains(self.client_a.get(self.result_url), "Good depth.")
        self.assertEqual(candidate_feedback(self.exam, self.attempt()), [])

    def test_the_answer_sheet_carries_the_comments_for_the_candidate_and_the_trainer(self):
        self.mark(**{f"comment_{self.q_open.id}": "Mention lease times.", f"comment_{self.q_mcq.id}": "Spot on.",
                     f"mark_{self.q_open.id}": "3"})
        a = self.attempt()
        mine = evidence.build_context(a, trainer=False)["ev"]
        self.assertTrue(mine["has_comments"])
        self.assertEqual({r["comment"] for r in mine["rows"] if r["comment"]}, {"Mention lease times.", "Spot on."})
        html = render_to_string("assessments/answer_sheet.html",
                                evidence.apply_density(evidence.build_context(a, trainer=False), evidence.DENSITIES[0]))
        self.assertIn("Trainer feedback", html)
        self.assertIn("Mention lease times.", html)
        self.assertNotIn("PRIVATE overall note", html)                       # only the trainer's copy has it
        self.assertNotIn("Mentions leases and automatic addressing", html)   # nor the marking guide
        theirs = render_to_string("assessments/answer_sheet.html",
                                  evidence.apply_density(evidence.build_context(a, trainer=True), evidence.DENSITIES[0]))
        self.assertIn("PRIVATE overall note", theirs)

    def test_the_sheet_before_marking_has_no_feedback_box(self):
        self.staff.post(reverse("assessments:attempt_mark", args=[self.exam.pk, self.attempt().pk]),
                        {f"comment_{self.q_mcq.id}": "Not for you yet."})
        a = self.attempt()
        self.assertFalse(evidence.build_context(a, trainer=False)["ev"]["has_comments"])
        self.assertTrue(evidence.build_context(a, trainer=True)["ev"]["has_comments"])

    @unittest.skipUnless(_pdf_engine(), "needs the WeasyPrint PDF engine")
    def test_the_candidates_pdf_is_one_page_with_feedback(self):
        self.mark(**{f"comment_{self.q_open.id}": "Mention lease times. " * 30, f"mark_{self.q_open.id}": "3"})
        a = self.attempt()
        self.assertEqual(len(evidence.render_sheet(evidence.build_context(a, trainer=False)).pages), 1)
        sheet = self.client_a.get(reverse("assessments:answer_sheet", args=[a.access_key]))
        self.assertEqual(sheet["Content-Type"], "application/pdf")
