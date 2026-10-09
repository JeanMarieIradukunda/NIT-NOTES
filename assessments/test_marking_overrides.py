"""Trainer marking of multiple-choice and fill-in questions, and a comment on each question."""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import Client
from django.urls import reverse

from . import analysis, services
from .marking import right_id
from .tests import SECRET_FILL, ExamTestBase

User = get_user_model()


class MarkingOverrideTests(ExamTestBase):
    """make_exam: MCQ 4 marks over 2 questions (2 each), fill 2, match 2, open 4."""

    def setUp(self):
        super().setUp()
        self.staff = Client()
        self.staff.login(username="trainer1", password="pw12345!")

    def submit_attempt(self):
        _, cfg = self.open_page()
        self.call(self.client_a, cfg, "start")
        m = self.qs[("match", 1)]
        answers = {
            str(self.qs[("mcq", 1)].id): 1,                                   # right
            str(self.qs[("mcq", 2)].id): 0,                                   # wrong
            str(self.qs[("fill", 1)].id): "zyxwv-secret-answr",              # a typo of the accepted answer
            str(m.id): {"0": right_id(m.id, 0), "1": right_id(m.id, 2)},      # one right, one wrong
            str(self.qs[("open", 1)].id): "Hands out IP addresses.",
        }
        self.assertTrue(self.call(self.client_a, cfg, "submit", answers=answers).json()["submitted"])
        a = self.attempt()
        self.assertEqual(a.objective_score, Decimal("3.00"))                  # 2 + 0 + 0 + 1
        return a

    def save(self, a, **fields):
        return self.staff.post(reverse("assessments:attempt_mark", args=[self.exam.pk, a.pk]), fields)

    def qid(self, section, n=1):
        return self.qs[(section, n)].id

    def test_adjust_mcq_and_fill_marks_changes_scores(self):
        a = self.submit_attempt()
        r = self.save(a, **{f"omark_{self.qid('mcq', 2)}": "2", f"omark_{self.qid('fill')}": "1.5"})
        self.assertEqual(r.status_code, 302)
        a = self.attempt()
        self.assertEqual(a.mark_overrides, {str(self.qid("mcq", 2)): "2.00", str(self.qid("fill")): "1.50"})
        self.assertEqual(a.objective_score, Decimal("6.50"))                  # 2 + 2 + 1.5 + 1
        self.assertEqual(Decimal(a.section_scores["mcq"]), Decimal("4.00"))
        self.assertEqual(Decimal(a.section_scores["fill"]), Decimal("1.50"))
        self.assertEqual(a.final_score, Decimal("6.50"))

    def test_adjusted_marks_combine_with_open_marks_and_penalty(self):
        a = self.submit_attempt()
        a.penalty_total = Decimal("1.00")
        a.save()
        self.save(a, **{f"omark_{self.qid('fill')}": "2", f"mark_{self.qid('open')}": "3"})
        a = self.attempt()
        self.assertEqual(a.objective_score, Decimal("5.00"))                  # 2 + 0 + 2 + 1
        self.assertEqual(a.final_score, Decimal("7.00"))                      # 5 + 3 open - 1 penalty

    def test_value_equal_to_automatic_mark_is_not_stored_and_clearing_goes_back_to_auto(self):
        a = self.submit_attempt()
        self.save(a, **{f"omark_{self.qid('mcq', 1)}": "2", f"omark_{self.qid('mcq', 2)}": "1"})
        a = self.attempt()
        self.assertEqual(a.mark_overrides, {str(self.qid("mcq", 2)): "1.00"})   # mcq 1 already scored 2
        self.assertEqual(a.objective_score, Decimal("4.00"))
        self.save(a, **{f"omark_{self.qid('mcq', 2)}": ""})                     # cleared
        a = self.attempt()
        self.assertEqual(a.mark_overrides, {})
        self.assertEqual(a.objective_score, Decimal("3.00"))

    def test_bad_values_are_rejected_and_nothing_is_saved(self):
        a = self.submit_attempt()
        for bad in ("3", "-1", "abc", "NaN", "Infinity"):
            self.save(a, **{f"omark_{self.qid('mcq', 2)}": bad, f"comment_{self.qid('mcq', 1)}": "should not stick"})
            a = self.attempt()
            self.assertEqual((a.mark_overrides, a.question_comments, a.objective_score), ({}, {}, Decimal("3.00")), bad)

    def test_matching_cannot_be_adjusted_and_unknown_ids_are_ignored(self):
        a = self.submit_attempt()
        self.save(a, **{f"omark_{self.qid('match')}": "2", "omark_999999": "1", "comment_999999": "ghost"})
        a = self.attempt()
        self.assertEqual((a.mark_overrides, a.question_comments, a.objective_score), ({}, {}, Decimal("3.00")))

    def test_comments_on_every_question_type_are_saved_and_shown(self):
        a = self.submit_attempt()
        fields = {f"comment_{self.qid(s, n)}": f"note on {s}{n}" for s, n in
                  (("mcq", 1), ("mcq", 2), ("fill", 1), ("match", 1), ("open", 1))}
        self.save(a, **fields)
        a = self.attempt()
        self.assertEqual(len(a.question_comments), 5)
        page = self.staff.get(reverse("assessments:attempt_detail", args=[self.exam.pk, a.pk]))
        for s, n in (("mcq", 1), ("fill", 1), ("match", 1), ("open", 1)):
            self.assertContains(page, f"note on {s}{n}")
        self.assertContains(page, "Save marks and comments")
        self.save(a, **{**fields, f"comment_{self.qid('mcq', 1)}": "   "})      # a blank comment removes it
        self.assertNotIn(str(self.qid("mcq", 1)), self.attempt().question_comments)

    def test_candidate_sees_comments_and_adjustment_only_when_answers_are_released(self):
        a = self.submit_attempt()
        self.save(a, **{f"omark_{self.qid('fill')}": "2", f"comment_{self.qid('fill')}": "Accepted: small typo.",
                        f"comment_{self.qid('open')}": "Add an example next time."})
        url = reverse("assessments:answer_review", args=[self.attempt().access_key])
        blocked = self.client_a.get(url)
        self.assertEqual(blocked.status_code, 403)
        self.assertNotContains(blocked, "small typo", status_code=403)
        self.exam.show_answers = True
        self.exam.save()
        r = self.client_a.get(url)
        self.assertContains(r, "Accepted: small typo.")
        self.assertContains(r, "Add an example next time.")
        self.assertContains(r, "Adjusted by your trainer")
        row = next(x for x in r.context["rows"] if x["section"] == "fill")
        self.assertEqual((row["earned"], row["status"], row["adjusted"]), (Decimal("2.00"), "correct", True))
        # the candidate's result page uses the updated section totals
        res = self.client_a.get(reverse("assessments:result", args=[a.access_key]))
        self.assertContains(res, "2 / 2")

    def test_cannot_mark_before_submission_and_only_owner_can(self):
        _, cfg = self.open_page()
        self.call(self.client_a, cfg, "start")
        a = self.attempt()
        self.save(a, **{f"comment_{self.qid('mcq', 1)}": "too early"})
        self.assertEqual(self.attempt().question_comments, {})
        self.assertNotContains(self.staff.get(reverse("assessments:attempt_detail", args=[self.exam.pk, a.pk])),
                               "Comment on this question")
        a = self.submit_after_start(cfg)
        other = User.objects.create_user("trainer2", password="pw12345!")
        other.groups.add(Group.objects.get(name="Trainer"))
        c2 = Client(); c2.login(username="trainer2", password="pw12345!")
        r = c2.post(reverse("assessments:attempt_mark", args=[self.exam.pk, a.pk]),
                    {f"omark_{self.qid('mcq', 2)}": "2", f"comment_{self.qid('mcq', 1)}": "hacked"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual((self.attempt().mark_overrides, self.attempt().question_comments), ({}, {}))

    def submit_after_start(self, cfg):
        self.call(self.client_a, cfg, "submit", answers={str(self.qid("mcq", 1)): 1})
        return self.attempt()

    def test_item_analysis_counts_the_adjusted_mark(self):
        a = self.submit_attempt()
        before = next(i for i in analysis.analyse(self.exam)[1] if i["section"] == "fill")
        self.assertEqual(before["full"], 0)
        self.save(a, **{f"omark_{self.qid('fill')}": "2"})
        after = next(i for i in analysis.analyse(self.exam)[1] if i["section"] == "fill")
        self.assertEqual(after["full"], 1)
        self.assertEqual(self.staff.get(reverse("assessments:exam_analysis", args=[self.exam.pk])).status_code, 200)

    def test_another_chance_clears_adjustments_and_comments(self):
        a = self.submit_attempt()
        self.save(a, **{f"omark_{self.qid('fill')}": "2", f"comment_{self.qid('open')}": "keep this"})
        self.assertTrue(self.attempt().mark_overrides)
        services.grant_retake(self.attempt())
        a = self.attempt()
        self.assertEqual((a.mark_overrides, a.question_comments, a.objective_score), ({}, {}, Decimal("0")))
