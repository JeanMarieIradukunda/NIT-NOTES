"""More time, removed deductions, power-outage recovery, and automatic feedback on the candidate's mark sheet."""

from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.template.loader import render_to_string
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from . import automark, evidence, services
from .models import Attempt
from .test_automark import GOOD, GUIDE
from .tests import ExamTestBase


class RecoveryBase(ExamTestBase):
    def setUp(self):
        super().setUp()
        self.staff = Client()
        self.staff.login(username="trainer1", password="pw12345!")
        self.open1 = self.qs[("open", 1)]
        self.open1.key.data = {"guide": GUIDE}
        self.open1.key.save()

    def start(self, reg="R1", name="Rita", client=None):
        c = client or Client()
        _, cfg = self.open_page(c, reg_no=reg, name=name)
        self.call(c, cfg, "start")
        return c, cfg, Attempt.objects.get(exam=self.exam, reg_no__iexact=reg)

    def submit(self, c, cfg, answers=None):
        self.call(c, cfg, "submit", answers=answers or {str(self.open1.id): GOOD})
        return Attempt.objects.get(exam=self.exam, reg_no__iexact=cfg_reg(cfg, self))

    def violate(self, a, n=2, vtype="fullscreen_exit"):
        for i in range(n):
            services.record_violation(Attempt.objects.get(pk=a.pk), vtype, episode=f"e{i}")
        return Attempt.objects.get(pk=a.pk)

    def fresh(self, a):
        return Attempt.objects.get(pk=a.pk)


def cfg_reg(cfg, test):
    return test.attempt().reg_no


class WaivedDeductionTests(RecoveryBase):
    def test_removing_deductions_updates_the_score_and_keeps_the_log(self):
        c, cfg, a = self.start()
        a = self.violate(a, 2)
        self.assertEqual((a.violation_count, a.penalty_total), (2, Decimal("4.00")))
        self.assertEqual(services.waive_violations(a, by="trainer1"), 2)
        a = self.fresh(a)
        self.assertEqual((a.violation_count, a.penalty_total), (0, Decimal("0.00")))
        self.assertEqual(a.violations.filter(waived=True).count(), 2)       # records stay for the evidence sheet
        self.assertEqual(a.recovery_log[-1]["action"], "deductions removed")
        self.assertEqual(services.waive_violations(a, waive=False), 2)       # and can be brought back
        self.assertEqual(self.fresh(a).penalty_total, Decimal("4.00"))

    def test_one_violation_can_be_removed_on_its_own(self):
        c, cfg, a = self.start()
        a = self.violate(a, 2)
        first = a.violations.order_by("pk").first()
        services.waive_violations(a, vids=[first.pk])
        a = self.fresh(a)
        self.assertEqual((a.violation_count, a.penalty_total), (1, Decimal("2.00")))

    def test_the_final_score_of_a_submitted_attempt_follows(self):
        c, cfg, a = self.start()
        a = self.violate(a, 1)
        self.call(c, cfg, "submit", answers={str(self.qs[("mcq", 1)].id): 1})
        a = self.fresh(a)
        before = a.final_score
        services.waive_violations(a)
        self.assertEqual(self.fresh(a).final_score, before + Decimal("2.00"))

    def test_removing_deductions_lowers_the_count_that_triggers_auto_submit(self):
        self.exam.max_violations = 3
        self.exam.save()
        c, cfg, a = self.start()
        a = self.violate(a, 2)
        services.waive_violations(a)
        a = self.violate(self.fresh(a), 1, "tab_switch")
        self.assertFalse(a.is_submitted)                                    # would have been the 3rd

    def test_the_view_and_the_page(self):
        c, cfg, a = self.start()
        a = self.violate(a, 2)
        url = reverse("assessments:attempt_waive", args=[self.exam.pk, a.pk])
        page = self.staff.get(reverse("assessments:attempt_detail", args=[self.exam.pk, a.pk]))
        self.assertContains(page, "Remove all deductions")
        self.staff.post(url, {"action": "waive"})
        page = self.staff.get(reverse("assessments:attempt_detail", args=[self.exam.pk, a.pk]))
        self.assertContains(page, "deduction removed")
        self.assertContains(page, "Restore deductions")
        self.staff.post(url, {"action": "restore"})
        self.assertEqual(self.fresh(a).penalty_total, Decimal("4.00"))

    def test_other_trainers_cannot_change_deductions(self):
        c, cfg, a = self.start()
        a = self.violate(a, 1)
        other = Client()
        other.login(username="trainer2", password="pw12345!")
        other.post(reverse("assessments:attempt_waive", args=[self.exam.pk, a.pk]), {"action": "waive"})
        self.assertEqual(self.fresh(a).penalty_total, Decimal("2.00"))

    def test_the_evidence_sheet_shows_a_removed_deduction(self):
        c, cfg, a = self.start()
        a = self.violate(a, 1)
        self.call(c, cfg, "submit", answers={})
        services.waive_violations(self.fresh(a))
        html = render_to_string("assessments/answer_sheet.html",
                                evidence.apply_density(evidence.build_context(self.fresh(a), trainer=True), evidence.DENSITIES[0]))
        self.assertIn("deduction removed", html)


class ExtendTimeTests(RecoveryBase):
    def test_one_candidate_gets_more_time(self):
        c, cfg, a = self.start()
        end = a.end_at
        services.extend_attempt(a, 10, by="t")
        a = self.fresh(a)
        self.assertEqual((a.end_at - end, a.extra_minutes), (timedelta(minutes=10), 10))

    def test_a_deadline_that_already_passed_restarts_from_now(self):
        c, cfg, a = self.start()
        Attempt.objects.filter(pk=a.pk).update(end_at=timezone.now() - timedelta(minutes=5))
        services.extend_attempt(self.fresh(a), 10)
        left = self.fresh(a).end_at - timezone.now()
        self.assertTrue(timedelta(minutes=9) < left <= timedelta(minutes=10))

    def test_time_can_be_added_before_the_candidate_starts(self):
        c = Client()
        _, cfg = self.open_page(c, reg_no="R9", name="Early")
        a = Attempt.objects.get(exam=self.exam, reg_no__iexact="R9")
        services.extend_attempt(a, 15)                                       # added before the clock starts
        self.call(c, cfg, "start")
        a = self.fresh(a)
        self.assertEqual((a.end_at - a.started_at), timedelta(minutes=self.exam.duration_minutes + 15))

    def test_a_working_candidate_sees_the_new_deadline_on_the_next_check_in(self):
        c, cfg, a = self.start()
        before = self.call(c, cfg, "heartbeat").json()["endAtMs"]
        services.extend_attempt(a, 10)
        after = self.call(c, cfg, "heartbeat").json()["endAtMs"]
        self.assertEqual(after - before, 10 * 60 * 1000)

    def test_limits_and_submitted_attempts(self):
        c, cfg, a = self.start()
        for bad in (0, -5, 241):
            with self.assertRaises(ValueError):
                services.extend_attempt(a, bad)
        self.call(c, cfg, "submit", answers={})
        with self.assertRaises(ValueError):
            services.extend_attempt(self.fresh(a), 10)

    def test_the_assessment_can_be_extended_and_running_candidates_follow(self):
        c, cfg, running = self.start("R1")
        done_c, done_cfg, done = self.start("R2", "Done")
        self.call(done_c, done_cfg, "submit", answers={})
        old_end = running.end_at
        n = services.extend_exam(self.exam, 20, include_running=True)
        self.exam.refresh_from_db()
        self.assertEqual((n, self.exam.duration_minutes), (1, 80))
        self.assertEqual(self.fresh(running).end_at - old_end, timedelta(minutes=20))
        self.assertEqual(self.fresh(done).end_at, done.end_at)              # submitted candidates untouched
        late = Client()
        _, lcfg = self.open_page(late, reg_no="R3", name="Late")
        self.call(late, lcfg, "start")
        a3 = Attempt.objects.get(exam=self.exam, reg_no__iexact="R3")
        self.assertEqual(a3.end_at - a3.started_at, timedelta(minutes=80))   # new starters get the longer time

    def test_running_candidates_can_be_left_out(self):
        c, cfg, running = self.start()
        self.assertEqual(services.extend_exam(self.exam, 5, include_running=False), 0)
        self.assertEqual(self.fresh(running).end_at, running.end_at)

    def test_views_and_page(self):
        c, cfg, a = self.start()
        self.assertContains(self.staff.get(reverse("assessments:exam_time", args=[self.exam.pk])), "Extend the assessment time")
        self.staff.post(reverse("assessments:attempt_extend", args=[self.exam.pk, a.pk]), {"minutes": "7"})
        self.assertEqual(self.fresh(a).extra_minutes, 7)
        r = self.staff.post(reverse("assessments:exam_extend", args=[self.exam.pk]), {"minutes": "10", "include_running": "1"}, follow=True)
        self.assertContains(r, "got the extra time automatically")
        r = self.staff.post(reverse("assessments:exam_extend", args=[self.exam.pk]), {"minutes": "999"}, follow=True)
        self.assertContains(r, "Add between 1 and 240")
        other = Client()
        other.login(username="trainer2", password="pw12345!")
        before = self.fresh(a).extra_minutes
        other.post(reverse("assessments:attempt_extend", args=[self.exam.pk, a.pk]), {"minutes": "30"})
        self.assertEqual(self.fresh(a).extra_minutes, before)


class ResumeAfterOutageTests(RecoveryBase):
    def timed_out(self, reg="R1"):
        c, cfg, a = self.start(reg, f"Cand {reg}")
        self.call(c, cfg, "save", answers={str(self.open1.id): GOOD}) if "save" in cfg["api"] else None
        self.call(c, cfg, "autosave", answers={str(self.open1.id): GOOD})
        Attempt.objects.filter(pk=a.pk).update(end_at=timezone.now() - timedelta(minutes=30))
        services.expire_if_due(self.fresh(a))
        return c, cfg, self.fresh(a)

    def test_an_attempt_that_timed_out_can_be_resumed_with_its_answers(self):
        c, cfg, a = self.timed_out()
        self.assertEqual((a.status, a.submit_reason), ("submitted", "time"))
        services.resume_attempt(a, 20, by="t")
        a = self.fresh(a)
        self.assertEqual((a.status, a.submit_reason, a.final_score, a.extra_minutes), ("in_progress", "", None, 20))
        self.assertTrue(timedelta(minutes=19) < a.end_at - timezone.now() <= timedelta(minutes=20))
        self.assertEqual(a.answers[str(self.open1.id)], GOOD)               # saved answers survive the outage
        _, cfg2 = self.open_page(Client(), reg_no="R1", name="Cand R1")     # and the candidate can get back in
        self.assertTrue(cfg2["tabToken"])

    def test_it_then_finishes_normally(self):
        c, cfg, a = self.timed_out()
        services.resume_attempt(a, 20)
        c2 = Client()
        _, cfg2 = self.open_page(c2, reg_no="R1", name="Cand R1")
        self.assertTrue(self.call(c2, cfg2, "submit", answers={}).json()["submitted"])
        self.assertEqual(self.fresh(a).submit_reason, "manual")

    def test_deductions_can_be_removed_when_resuming(self):
        c, cfg, a = self.start()
        a = self.violate(a, 2)
        Attempt.objects.filter(pk=a.pk).update(end_at=timezone.now() - timedelta(minutes=30))
        services.expire_if_due(self.fresh(a))
        services.resume_attempt(self.fresh(a), 10, waive=True)
        a = self.fresh(a)
        self.assertEqual((a.violation_count, a.penalty_total), (0, Decimal("0.00")))

    def test_attempts_a_trainer_has_marked_or_the_candidate_submitted_are_not_resumed(self):
        c, cfg, a = self.timed_out()
        a.open_marks = {str(self.open1.id): "2"}                            # a trainer's mark
        a.save()
        with self.assertRaisesMessage(ValueError, "already marked"):
            services.resume_attempt(self.fresh(a), 10)
        c2, cfg2, b = self.start("R2", "Manual")
        self.call(c2, cfg2, "submit", answers={})
        with self.assertRaisesMessage(ValueError, "submitted automatically"):
            services.resume_attempt(self.fresh(b), 10)

    def test_automatic_marks_are_discarded_and_redone_on_the_next_submit(self):
        c, cfg, a = self.timed_out()
        automark.automark_attempt(self.fresh(a))
        self.assertTrue(self.fresh(a).open_marks)
        services.resume_attempt(self.fresh(a), 10)
        a = self.fresh(a)
        self.assertEqual((a.open_marks, a.question_comments, a.marking_complete), ({}, {}, False))

    def test_the_marking_screen_offers_resume_and_the_view_works(self):
        c, cfg, a = self.timed_out()
        page = self.staff.get(reverse("assessments:attempt_detail", args=[self.exam.pk, a.pk]))
        self.assertContains(page, "Resume attempt")
        r = self.staff.post(reverse("assessments:attempt_resume", args=[self.exam.pk, a.pk]), {"minutes": "25"}, follow=True)
        self.assertContains(r, "can enter again")
        self.assertEqual(self.fresh(a).status, "in_progress")


class OutageRecoveryTests(RecoveryBase):
    def test_one_action_gets_everyone_back(self):
        c1, cfg1, working = self.start("R1")
        for _ in range(1):
            self.call(c1, cfg1, "heartbeat")
        c2, cfg2, expired = self.start("R2")
        Attempt.objects.filter(pk=expired.pk).update(end_at=timezone.now() - timedelta(minutes=20), status="submitted",
                                                     submit_reason="time", submitted_at=timezone.now() - timedelta(minutes=10))
        a = self.violate(working, 2)
        end = a.end_at
        res = services.recover_exam(self.exam, minutes=15, resume_hours=3, waive=True, by="t")
        self.assertEqual((res["reset"], res["extended"], res["resumed"], res["waived"]), (1, 1, 1, 1))
        working = self.fresh(working)
        self.assertEqual(working.end_at - end, timedelta(minutes=15))
        self.assertEqual(working.penalty_total, Decimal("0.00"))
        self.assertEqual(working.devices.count(), 0)                         # opens and devices reset: they can re-enter
        self.assertEqual(self.fresh(expired).status, "in_progress")

    def test_the_look_back_window_and_blocked_candidates(self):
        c, cfg, old = self.start("R1")
        Attempt.objects.filter(pk=old.pk).update(status="submitted", submit_reason="time",
                                                 submitted_at=timezone.now() - timedelta(hours=10))
        c2, cfg2, marked = self.start("R2")
        Attempt.objects.filter(pk=marked.pk).update(status="submitted", submit_reason="time", submitted_at=timezone.now(),
                                                    open_marks={str(self.open1.id): "2"})
        preview = services.recovery_preview(self.exam, 3)
        self.assertEqual(([a.reg_no for a in preview["resumable"]], [a.reg_no for a in preview["blocked"]]), ([], [marked.reg_no]))
        res = services.recover_exam(self.exam, minutes=10, resume_hours=3)
        self.assertEqual((res["resumed"], res["skipped"]), (0, []))
        self.assertEqual(len(services.recovery_preview(self.exam, 24)["resumable"]), 1)

    def test_each_part_is_optional(self):
        c, cfg, a = self.start()
        end = a.end_at
        res = services.recover_exam(self.exam, minutes=10, extend_running=False, reset_running=False)
        self.assertEqual((res["reset"], res["extended"], res["resumed"]), (0, 0, 0))
        self.assertEqual(self.fresh(a).end_at, end)

    def test_page_and_views(self):
        c, cfg, a = self.start()
        page = self.staff.get(reverse("assessments:exam_time", args=[self.exam.pk]) + "?hours=6")
        for needle in ("Recover after a power or network cut", "Let them back in", "Resume auto-submitted attempts", "Run recovery"):
            self.assertContains(page, needle)
        r = self.staff.post(reverse("assessments:exam_recover", args=[self.exam.pk]),
                            {"minutes": "10", "reset_running": "1", "extend_running": "1", "hours": "3"}, follow=True)
        self.assertContains(r, "Recovery done")
        self.assertContains(r, "access reset for 1")
        other = Client()
        other.login(username="trainer2", password="pw12345!")
        end = self.fresh(a).end_at
        other.post(reverse("assessments:exam_recover", args=[self.exam.pk]), {"minutes": "10", "extend_running": "1"})
        self.assertEqual(self.fresh(a).end_at, end)

    def test_the_menus_link_to_the_page(self):
        for name in ("assessments:exam_detail", "assessments:exam_results"):
            self.assertContains(self.staff.get(reverse(name, args=[self.exam.pk])), "Time and outage recovery")


class AutomaticFeedbackTests(RecoveryBase):
    def marked(self):
        c, cfg, a = self.start()
        self.call(c, cfg, "submit", answers={str(self.open1.id): GOOD})
        a = self.fresh(a)
        automark.automark_attempt(a)
        return c, self.fresh(a)

    def test_why_this_mark_becomes_the_comment_on_the_candidates_mark_sheet(self):
        with mock.patch.object(automark, "ai_mark", return_value=(Decimal("3"), "AI: You explained leases well, but not the server.")):
            c, a = self.marked()
            automark.automark_attempt(a, method="ai", overwrite=True)
        a = self.fresh(a)
        self.assertEqual(a.question_comments[str(self.open1.id)], "You explained leases well, but not the server.")
        mine = evidence.build_context(a, trainer=False)["ev"]
        row = [r for r in mine["rows"] if r["comment"]][0]
        self.assertEqual((row["comment"], row["comment_auto"]), ("You explained leases well, but not the server.", True))
        html = render_to_string("assessments/answer_sheet.html",
                                evidence.apply_density(evidence.build_context(a, trainer=False), evidence.DENSITIES[0]))
        self.assertIn("You explained leases well", html)
        page = c.get(reverse("assessments:result", args=[a.access_key])).content.decode()
        self.assertIn("You explained leases well", page)
        self.assertIn("Automatic feedback", page)

    def test_key_point_feedback_never_quotes_the_marking_guide(self):
        c, a = self.marked()
        text = a.question_comments[str(self.open1.id)]
        self.assertIn("key points", text)
        for secret in ("leases", "DHCP server", "Assigns", "Missing"):
            self.assertNotIn(secret, text)

    def test_a_trainers_own_comment_is_never_replaced_but_an_automatic_one_is_refreshed(self):
        c, a = self.marked()
        qid = str(self.open1.id)
        a.question_comments[qid] = "My own words for you."
        a.save()
        automark.automark_attempt(self.fresh(a), overwrite=True)
        self.assertEqual(self.fresh(a).question_comments[qid], "My own words for you.")
        b = self.fresh(a)
        b.question_comments[qid] = b.open_marking[f"fb:{qid}"]               # back to the automatic text
        b.save()
        self.open1.key.data = {"guide": "- Cables\n- Routers"}
        self.open1.key.save()
        automark.automark_attempt(self.fresh(a), overwrite=True)
        self.assertNotEqual(self.fresh(a).question_comments[qid], "My own words for you.")
        self.assertIn("did not cover", self.fresh(a).question_comments[qid])

    def test_saving_marks_keeps_the_automatic_comment_and_editing_it_makes_it_the_trainers(self):
        c, a = self.marked()
        qid = str(self.open1.id)
        auto_text = a.question_comments[qid]
        url = reverse("assessments:attempt_mark", args=[self.exam.pk, a.pk])
        self.staff.post(url, {f"mark_{qid}": a.open_marks[qid], f"comment_{qid}": auto_text})
        a = self.fresh(a)
        self.assertEqual(a.open_marking[f"fb:{qid}"], auto_text)             # still known to be automatic
        self.staff.post(url, {f"mark_{qid}": a.open_marks[qid], f"comment_{qid}": "Edited by me."})
        a = self.fresh(a)
        mine = evidence.build_context(a, trainer=False)["ev"]
        self.assertFalse([r for r in mine["rows"] if r["comment"]][0]["comment_auto"])

    def test_the_marking_screen_says_where_the_comment_came_from(self):
        c, a = self.marked()
        page = self.staff.get(reverse("assessments:attempt_detail", args=[self.exam.pk, a.pk]))
        self.assertContains(page, "Written automatically from")

    def test_blank_answers_get_a_plain_sentence(self):
        c, cfg, a = self.start()
        self.call(c, cfg, "submit", answers={})
        automark.automark_attempt(self.fresh(a))
        self.assertEqual(self.fresh(a).question_comments[str(self.open1.id)], "You did not answer this question.")

    def test_comments_still_wait_for_results_to_be_released(self):
        c, a = self.marked()
        self.exam.show_results = False
        self.exam.save()
        self.assertNotIn(a.question_comments[str(self.open1.id)],
                         c.get(reverse("assessments:result", args=[a.access_key])).content.decode())
