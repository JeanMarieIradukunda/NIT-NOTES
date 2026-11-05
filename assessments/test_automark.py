"""Automatic marking of open questions, and a trainer's ability to review and re-mark them."""

from decimal import Decimal
from unittest import mock

from django.test import Client
from django.urls import reverse

from . import automark
from .models import AnswerKey, Question
from .tests import ExamTestBase

GUIDE = "- Assigns IP addresses automatically\n- Uses leases that expire and are renewed\n- Runs on a DHCP server"
GOOD = "A DHCP server automatically gives IP addresses to hosts. The addresses are leased and renewed."


class KeywordMarkerTests(ExamTestBase):
    def test_marks_follow_the_key_points_found(self):
        full, note = automark.keyword_mark(GUIDE, GOOD, Decimal("6"))
        self.assertEqual(full, Decimal("6.00"))
        self.assertIn("covers 3 of 3", note)
        some, _ = automark.keyword_mark(GUIDE, "It hands out IP addresses automatically.", Decimal("6"))
        self.assertTrue(Decimal("0") < some < Decimal("6"))
        self.assertEqual(automark.keyword_mark(GUIDE, "Cats are nice.", Decimal("6"))[0], Decimal("0.00"))

    def test_edge_cases(self):
        self.assertEqual(automark.keyword_mark(GUIDE, "  ", Decimal("4")), (Decimal("0"), "No answer."))
        value, note = automark.keyword_mark("", GOOD, Decimal("4"))
        self.assertIsNone(value)
        self.assertIn("No marking guide", note)

    def test_plurals_and_endings_agree(self):
        self.assertEqual(automark.stem("addresses"), automark.stem("addressing"))
        self.assertEqual(automark.stem("address"), automark.stem("addresses"))
        self.assertEqual(automark.stem("automatically"), automark.stem("automatic"))

    def test_never_above_the_maximum_and_steps_are_tidy(self):
        for answer in (GOOD, GOOD * 3, "DHCP"):
            value, _ = automark.keyword_mark(GUIDE, answer, Decimal("2.5"))
            self.assertLessEqual(value, Decimal("2.5"))
            self.assertEqual(value % Decimal("0.25"), 0)


class AutoMarkFlowTests(ExamTestBase):
    def setUp(self):
        super().setUp()
        self.staff = Client()
        self.staff.login(username="trainer1", password="pw12345!")
        self.open1 = self.qs[("open", 1)]
        self.open1.key.data = {"guide": GUIDE}
        self.open1.key.save()
        _, cfg = self.open_page()
        self.call(self.client_a, cfg, "start")
        self.call(self.client_a, cfg, "submit", answers={str(self.open1.id): GOOD})

    def add_open(self, text="Second open question", guide="", marks=None):
        q = Question.objects.create(exam=self.exam, section="open", order=9, text=text, marks=marks)
        AnswerKey.objects.create(question=q, data={"guide": guide})
        return q

    def fresh(self):
        return self.attempt()

    def test_marks_are_stored_tagged_auto_and_the_score_follows(self):
        a = self.fresh()
        res = automark.automark_attempt(a)
        a = self.fresh()
        self.assertEqual(res["marked"], 1)
        self.assertEqual(Decimal(a.open_marks[str(self.open1.id)]), Decimal("4.00"))        # whole open section
        self.assertEqual(a.open_marking[str(self.open1.id)]["by"], "auto")
        self.assertTrue(a.marking_complete)
        self.assertEqual(a.auto_marked_count, 1)
        self.assertEqual(a.final_score, a.objective_score + Decimal("4.00") - a.penalty_total)

    def test_a_question_without_a_guide_is_left_for_the_trainer(self):
        extra = self.add_open()
        res = automark.automark_attempt(self.fresh())
        a = self.fresh()
        self.assertEqual((res["marked"], res["no_guide"]), (1, 1))
        self.assertNotIn(str(extra.id), a.open_marks)
        self.assertFalse(a.marking_complete)

    def test_trainer_marks_survive_a_rerun_unless_overwrite_is_asked_for(self):
        a = self.fresh()
        a.open_marks = {str(self.open1.id): "1.5"}                  # no entry in open_marking = a trainer's mark
        a.save()
        res = automark.automark_attempt(self.fresh())
        self.assertEqual((res["marked"], res["kept"]), (0, 1))
        self.assertEqual(Decimal(self.fresh().open_marks[str(self.open1.id)]), Decimal("1.5"))
        automark.automark_attempt(self.fresh(), overwrite=True)
        self.assertEqual(Decimal(self.fresh().open_marks[str(self.open1.id)]), Decimal("4.00"))

    def test_rerunning_refreshes_automatic_marks_after_the_guide_changes(self):
        automark.automark_attempt(self.fresh())
        self.open1.key.data = {"guide": "- Hands out IP addresses\n- Cables\n- Routers\n- Firewalls"}
        self.open1.key.save()
        automark.automark_attempt(self.fresh())
        self.assertLess(Decimal(self.fresh().open_marks[str(self.open1.id)]), Decimal("4"))

    def test_custom_question_marks_cap_the_automatic_mark(self):
        self.open1.marks = Decimal("2.5")
        self.open1.save()
        automark.automark_attempt(self.fresh())
        self.assertEqual(Decimal(self.fresh().open_marks[str(self.open1.id)]), Decimal("2.50"))

    def test_only_submitted_attempts_are_marked(self):
        a = self.fresh()
        a.status = a.IN_PROGRESS
        a.save()
        self.assertEqual(automark.automark_attempt(self.fresh())["marked"], 0)

    # -- AI --------------------------------------------------------------------------------------

    def test_ai_marks_are_used_clamped_and_failures_fall_back(self):
        with mock.patch.object(automark, "ai_mark", return_value=(Decimal("3"), "AI: good")) as m:
            automark.automark_attempt(self.fresh(), method="ai", overwrite=True)
        self.assertTrue(m.called)
        meta = self.fresh().open_marking[str(self.open1.id)]
        self.assertEqual((meta["method"], Decimal(self.fresh().open_marks[str(self.open1.id)])), ("ai", Decimal("3")))
        with mock.patch.object(automark, "ai_mark", side_effect=RuntimeError("down")):
            res = automark.automark_attempt(self.fresh(), method="ai", overwrite=True)
        self.assertEqual(res["ai_fallback"], 1)
        self.assertEqual(self.fresh().open_marking[str(self.open1.id)]["method"], "keywords")

    def test_ai_calls_are_capped_per_request(self):
        with mock.patch.object(automark, "ai_mark", return_value=(Decimal("1"), "AI: ok")):
            res = automark.automark_attempt(self.fresh(), method="ai", overwrite=True, ai_budget=[0])
        self.assertEqual((res["marked"], res["ai_limit"]), (0, 1))

    def test_ai_reply_is_parsed_and_clamped(self):
        with mock.patch("ai_tools.services.complete", return_value='Sure! {"marks": 99, "reason": "all points"}'), \
             mock.patch("ai_tools.services.is_configured", return_value=True):
            value, note = automark.ai_mark("Q", GUIDE, GOOD, Decimal("4"))
        self.assertEqual(value, Decimal("4.00"))
        self.assertTrue(note.startswith("AI:"))

    def test_the_ai_choice_is_ignored_when_no_key_is_configured(self):
        url = reverse("assessments:attempt_automark", args=[self.exam.pk, self.fresh().pk])
        with mock.patch.object(automark, "ai_mark") as m:
            self.staff.post(url, {"method": "ai"})
        self.assertFalse(m.called)
        self.assertEqual(self.fresh().open_marking[str(self.open1.id)]["method"], "keywords")

    # -- views -----------------------------------------------------------------------------------

    def test_attempt_and_class_auto_mark_views(self):
        r = self.staff.post(reverse("assessments:attempt_automark", args=[self.exam.pk, self.fresh().pk]), follow=True)
        self.assertContains(r, "Auto-marked 1 written answer")
        self.assertEqual(self.fresh().auto_marked_count, 1)
        a = self.fresh()
        a.open_marks, a.open_marking, a.marking_complete = {}, {}, False
        a.save()
        r = self.staff.post(reverse("assessments:exam_automark", args=[self.exam.pk]), follow=True)
        self.assertContains(r, "Auto-marked 1 written answer")
        self.assertTrue(self.fresh().marking_complete)

    def test_other_trainers_cannot_auto_mark(self):
        other = Client()
        other.login(username="trainer2", password="pw12345!")
        r = other.post(reverse("assessments:exam_automark", args=[self.exam.pk]))
        self.assertIn(r.status_code, (302, 403))
        self.assertEqual(self.fresh().auto_marked_count, 0)

    def mark_url(self):
        return reverse("assessments:attempt_mark", args=[self.exam.pk, self.fresh().pk])

    def test_an_automatic_mark_stays_automatic_until_a_trainer_changes_or_checks_it(self):
        automark.automark_attempt(self.fresh())
        self.staff.post(self.mark_url(), {f"mark_{self.open1.id}": "4"})            # saved unchanged
        self.assertEqual(self.fresh().auto_marked_count, 1)
        self.staff.post(self.mark_url(), {f"mark_{self.open1.id}": "3.5"})          # trainer changes it
        a = self.fresh()
        self.assertEqual(a.auto_marked_count, 0)
        self.assertEqual(Decimal(a.open_marks[str(self.open1.id)]), Decimal("3.5"))
        automark.automark_attempt(self.fresh(), overwrite=True)
        self.staff.post(self.mark_url(), {f"mark_{self.open1.id}": "4", "reviewed_all": "1"})   # checked as is
        self.assertEqual(self.fresh().auto_marked_count, 0)

    def test_save_and_next_goes_to_the_next_candidate_who_needs_marking(self):
        r = self.staff.post(self.mark_url(), {f"mark_{self.open1.id}": "4", "then": "next"})
        self.assertRedirects(r, reverse("assessments:exam_results", args=[self.exam.pk]))

    def test_auto_marking_on_submit_when_the_exam_asks_for_it(self):
        self.exam.auto_mark_on_submit = True
        self.exam.save()
        a = self.fresh()
        from . import services
        services.grant_retake(a)
        self.assertEqual(self.fresh().open_marking, {})                             # retake clears who-marked-what
        _, cfg = self.open_page()
        self.call(self.client_a, cfg, "start")
        self.call(self.client_a, cfg, "submit", answers={str(self.open1.id): GOOD})
        a = self.fresh()
        self.assertTrue(a.marking_complete)
        self.assertEqual(a.auto_marked_count, 1)

    def test_a_marking_failure_never_blocks_a_submission(self):
        self.exam.auto_mark_on_submit = True
        self.exam.save()
        from . import services
        services.grant_retake(self.fresh())
        _, cfg = self.open_page()
        self.call(self.client_a, cfg, "start")
        with mock.patch.object(automark, "automark_attempt", side_effect=RuntimeError("boom")):
            self.assertTrue(self.call(self.client_a, cfg, "submit", answers={}).json()["submitted"])
        self.assertTrue(self.fresh().is_submitted)

    # -- pages -----------------------------------------------------------------------------------

    def test_pages_show_the_new_organisation(self):
        automark.automark_attempt(self.fresh())
        results = self.staff.get(reverse("assessments:exam_results", args=[self.exam.pk]))
        for needle in ("Results &amp; marking", "Auto-mark (key points)", "Auto-marked · check", 'id="rtable"', "auto</span>"):
            self.assertContains(results, needle)
        detail = self.staff.get(reverse("assessments:attempt_detail", args=[self.exam.pk, self.fresh().pk]))
        for needle in ("Why this mark", "Re-run key points", "I have checked the automatic marks", "Marking guide", 'id="liveFinal"'):
            self.assertContains(detail, needle)
        overview = self.staff.get(reverse("assessments:exam_detail", args=[self.exam.pk]))
        for needle in ("Candidates entered", "Release and marking", "Rules and penalties"):
            self.assertContains(overview, needle)
        csv = self.staff.get(reverse("assessments:exam_results", args=[self.exam.pk]) + "?format=csv").content.decode()
        self.assertIn("Open marks by", csv)
        self.assertIn("auto", csv.splitlines()[1])

    def test_the_candidate_is_told_when_marks_were_automatic_but_not_the_explanation(self):
        automark.automark_attempt(self.fresh())
        page = self.client_a.get(reverse("assessments:result", args=[self.fresh().access_key])).content.decode()
        self.assertIn("marked automatically", page)
        self.assertNotIn("key points", page)
        self.assertNotIn("Missing:", page)

    def test_the_assessment_list_shows_how_many_wait_for_marking(self):
        a = self.fresh()
        a.marking_complete = False
        a.save()
        page = self.staff.get(reverse("assessments:exam_list"))
        self.assertContains(page, "To mark")


class AiRemarkTests(ExamTestBase):
    """"Re-mark with AI": only answers no trainer has marked, in batches, never twice in one run."""

    def setUp(self):
        super().setUp()
        self.staff = Client()
        self.staff.login(username="trainer1", password="pw12345!")
        self.open1 = self.qs[("open", 1)]
        self.open1.key.data = {"guide": GUIDE}
        self.open1.key.save()
        self.submit("S1", GOOD)

    def submit(self, reg, answer):
        c = Client()
        _, cfg = self.open_page(c, reg_no=reg, name=f"Cand {reg}")
        self.call(c, cfg, "start")
        self.call(c, cfg, "submit", answers={str(self.open1.id): answer})

    def attempts(self):
        return list(self.exam.attempts.order_by("pk"))

    def ai(self, value="3"):
        return mock.patch.object(automark, "ai_mark", return_value=(Decimal(value), "AI: You covered the main points."))

    def test_trainer_marks_are_never_changed_but_everything_else_is(self):
        a1 = self.attempts()[0]
        a1.open_marks = {str(self.open1.id): "1.5"}                      # a trainer's mark
        a1.save()
        self.submit("S2", GOOD)                                          # unmarked
        self.submit("S3", GOOD)
        a3 = self.attempts()[2]
        automark.automark_attempt(a3)                                    # marked by key points
        with self.ai() as m:
            res = automark.remark_with_ai(self.exam, include_ai=True)
        a1, a2, a3 = self.attempts()
        self.assertEqual(Decimal(a1.open_marks[str(self.open1.id)]), Decimal("1.5"))
        self.assertEqual(a1.open_marking, {})
        for a in (a2, a3):
            self.assertEqual(Decimal(a.open_marks[str(self.open1.id)]), Decimal("3.00"))
            self.assertEqual(a.open_marking[str(self.open1.id)]["method"], "ai")
            self.assertIn("at", a.open_marking[str(self.open1.id)])
        self.assertEqual((m.call_count, res["marked"], res["remaining"]), (2, 2, 0))

    def test_ai_marks_are_skipped_next_time_unless_asked_for(self):
        with self.ai() as m:
            automark.remark_with_ai(self.exam)
            automark.remark_with_ai(self.exam)                           # nothing left to do
        self.assertEqual(m.call_count, 1)
        with self.ai("2") as m:
            automark.remark_with_ai(self.exam, include_ai=True)
        self.assertEqual(m.call_count, 1)
        self.assertEqual(Decimal(self.attempts()[0].open_marks[str(self.open1.id)]), Decimal("2.00"))

    def test_batches_finish_and_never_mark_the_same_answer_twice_in_a_run(self):
        for i in range(2, 6):
            self.submit(f"S{i}", GOOD)                                   # five candidates
        with self.ai() as m:
            first = automark.remark_with_ai(self.exam, include_ai=True, limit=2)
            self.assertEqual((first["marked"], first["remaining"]), (2, 3))
            second = automark.remark_with_ai(self.exam, include_ai=True, since=first["since"], limit=2)
            third = automark.remark_with_ai(self.exam, include_ai=True, since=first["since"], limit=2)
        self.assertEqual((second["marked"], second["remaining"]), (2, 1))
        self.assertEqual((third["marked"], third["remaining"]), (1, 0))
        self.assertEqual(m.call_count, 5)

    def test_blank_answers_get_zero_without_using_the_ai(self):
        self.submit("S2", "")
        with self.ai() as m:
            res = automark.remark_with_ai(self.exam)
        self.assertEqual(m.call_count, 1)
        blank = self.attempts()[1]
        self.assertEqual(Decimal(blank.open_marks[str(self.open1.id)]), Decimal("0"))
        self.assertEqual(res["remaining"], 0)

    def test_an_unavailable_ai_stops_the_run_and_says_so(self):
        self.submit("S2", GOOD)
        with mock.patch.object(automark, "ai_mark", side_effect=RuntimeError("down")):
            res = automark.remark_with_ai(self.exam)
        self.assertEqual(res["ai_fallback"], 1)
        self.assertEqual(res["marked"], 1)                               # only the first answer, then it stopped

    def test_counts_for_the_confirmation_window(self):
        a1 = self.attempts()[0]
        a1.open_marks = {str(self.open1.id): "2"}
        a1.save()
        self.submit("S2", GOOD)
        counts = automark.count_eligible(self.exam)
        self.assertEqual((counts["eligible"], counts["trainer"], counts["ai_done"], counts["no_guide"]), (1, 1, 0, 0))

    def test_the_endpoint_returns_progress_as_json_and_needs_ai_to_be_set_up(self):
        url = reverse("assessments:exam_remark_ai", args=[self.exam.pk])
        r = self.staff.post(url, HTTP_ACCEPT="application/json")
        self.assertEqual(r.status_code, 400)
        self.assertIn("not set up", r.json()["error"])
        with mock.patch.object(automark, "ai_available", return_value=True), self.ai():
            r = self.staff.post(url, HTTP_ACCEPT="application/json")
        data = r.json()
        self.assertEqual((r.status_code, data["marked"], data["remaining"]), (200, 1, 0))
        self.assertTrue(data["since"])
        other = Client()
        other.login(username="trainer2", password="pw12345!")
        self.assertIn(other.post(url, HTTP_ACCEPT="application/json").status_code, (302, 403))

    def test_results_page_offers_the_button_and_explains_what_will_happen(self):
        page = reverse("assessments:exam_results", args=[self.exam.pk])
        html = self.staff.get(page).content.decode()
        self.assertIn("Re-mark with AI", html)
        self.assertNotIn('id="aiRemarkModal"', html)                     # disabled until a key is set
        with mock.patch.object(automark, "ai_available", return_value=True):
            html = self.staff.get(page).content.decode()
        for needle in ('id="aiRemarkModal"', "no trainer has marked", "kept as they are", "Start re-marking"):
            self.assertIn(needle, html)

    def test_the_marking_screen_ai_button_never_replaces_a_trainers_mark(self):
        a = self.attempts()[0]
        a.open_marks = {str(self.open1.id): "1"}
        a.save()
        url = reverse("assessments:attempt_automark", args=[self.exam.pk, a.pk])
        with mock.patch.object(automark, "ai_available", return_value=True), self.ai("4"):
            self.staff.post(url, {"method": "ai", "overwrite": "1"})
        self.assertEqual(Decimal(self.attempts()[0].open_marks[str(self.open1.id)]), Decimal("1"))


class AiTimeLimitTests(ExamTestBase):
    """A request must finish inside the host's 60 seconds: short AI timeouts, a time budget and a batch cap."""

    def setUp(self):
        super().setUp()
        self.open1 = self.qs[("open", 1)]
        self.open1.key.data = {"guide": GUIDE}
        self.open1.key.save()
        for i in range(4):
            c = Client()
            _, cfg = self.open_page(c, reg_no=f"T{i}", name=f"Cand {i}")
            self.call(c, cfg, "start")
            self.call(c, cfg, "submit", answers={str(self.open1.id): GOOD})

    def test_the_ai_call_gets_a_short_timeout_and_no_hidden_retries(self):
        fake = mock.MagicMock()
        fake.return_value.chat.completions.create.return_value.choices = [
            mock.MagicMock(message=mock.MagicMock(content='{"marks": 2, "reason": "You covered it."}'))]
        with mock.patch("ai_tools.services.is_configured", return_value=True), \
             mock.patch("groq.Groq", fake), self.settings(GROQ_API_KEY="k"):
            automark.ai_mark("Q", GUIDE, GOOD, Decimal("4"))
        _, kwargs = fake.call_args
        self.assertEqual((kwargs["timeout"], kwargs["max_retries"]), (automark.AI_CALL_TIMEOUT, 0))

    def test_other_ai_features_keep_the_library_defaults(self):
        fake = mock.MagicMock()
        fake.return_value.chat.completions.create.return_value.choices = [mock.MagicMock(message=mock.MagicMock(content="ok"))]
        with mock.patch("groq.Groq", fake), self.settings(GROQ_API_KEY="k"):
            from ai_tools import services
            services.complete("s", "u")
        self.assertNotIn("timeout", fake.call_args.kwargs)

    def test_the_default_batch_is_small_enough_for_a_slow_service(self):
        self.assertLessEqual(automark.AI_CALLS_PER_REQUEST, 10)
        self.assertLess(automark.AI_TIME_BUDGET + automark.AI_CALL_TIMEOUT, 60)

    def test_no_new_ai_call_starts_after_the_time_budget_is_used(self):
        times = iter([0, 0, 0, 100, 100, 100, 100, 100, 100, 100])       # the clock jumps past the budget after one call
        with mock.patch.object(automark.time, "monotonic", side_effect=lambda: next(times)), \
             mock.patch.object(automark, "ai_mark", return_value=(Decimal("3"), "AI: ok")) as m:
            res = automark.remark_with_ai(self.exam)
        self.assertEqual(m.call_count, 1)
        self.assertEqual(res["marked"], 1)
        self.assertEqual(res["remaining"], 3)                            # the rest wait for the next batch

    def test_a_batch_stops_at_its_call_cap_and_the_rest_follow_in_the_next(self):
        with mock.patch.object(automark, "ai_mark", return_value=(Decimal("3"), "AI: ok")) as m:
            first = automark.remark_with_ai(self.exam, limit=3)
            second = automark.remark_with_ai(self.exam, since=first["since"], limit=3)
        self.assertEqual((first["marked"], first["remaining"]), (3, 1))
        self.assertEqual((second["marked"], second["remaining"]), (1, 0))
        self.assertEqual(m.call_count, 4)

    def test_attempts_with_nothing_to_re_mark_are_not_even_loaded(self):
        with mock.patch.object(automark, "ai_mark", return_value=(Decimal("3"), "AI: ok")):
            automark.remark_with_ai(self.exam)                           # everything is AI-marked now
        with mock.patch.object(automark, "automark_attempt") as per_attempt:
            res = automark.remark_with_ai(self.exam)
        self.assertFalse(per_attempt.called)
        self.assertEqual((res["marked"], res["remaining"]), (0, 0))

    def test_a_timed_out_call_falls_back_and_stops_the_run(self):
        with mock.patch.object(automark, "ai_mark", side_effect=TimeoutError("slow")):
            res = automark.remark_with_ai(self.exam)
        self.assertEqual((res["ai_fallback"], res["marked"]), (1, 1))     # first answer by key points, then it stops
