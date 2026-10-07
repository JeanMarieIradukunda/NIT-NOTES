import json
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import services
from .marking import right_id
from .models import AnswerKey, Attempt, DeviceOpen, Exam, Question

User = get_user_model()
PLAIN_STATIC = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
PASSWORD = "Tr1cky-exam-pw"
SECRET_FILL = "zyxwv-secret-answer"


def make_exam(owner, **overrides):
    exam = Exam(title="Networking test", created_by=owner, is_open=True, duration_minutes=60,
                max_opens=2, max_violations=5, marks_mcq=Decimal("4"), marks_fill=Decimal("2"),
                marks_open=Decimal("4"), marks_match=Decimal("2"))
    for k, v in overrides.items():
        setattr(exam, k, v)
    exam.set_password(PASSWORD)
    exam.save()
    qs = {}
    def add(section, order, text, payload, key):
        q = Question.objects.create(exam=exam, section=section, order=order, text=text, payload=payload)
        AnswerKey.objects.create(question=q, data=key)
        qs[(section, order)] = q
    add("mcq", 1, "Which layer routes packets?", {"options": ["Physical", "Network", "Session"]}, {"correct": 1})
    add("mcq", 2, "Default HTTP port?", {"options": ["21", "80", "443"]}, {"correct": 1})
    add("fill", 1, "The ____ protocol resolves names.", {}, {"accepted": [SECRET_FILL, "alt answer"], "case_sensitive": False})
    add("match", 1, "Match the device", {"left": ["Router", "Switch"], "right": ["Connects networks", "Connects hosts", "Extra"]},
        {"pairs": {"0": 0, "1": 1}})
    add("open", 1, "Explain DHCP.", {}, {"guide": "Mentions leases and automatic addressing."})
    return exam, qs


@override_settings(STORAGES=PLAIN_STATIC)
class ExamTestBase(TestCase):
    def setUp(self):
        from django.contrib.auth.models import Group
        self.trainer = User.objects.create_user("trainer1", password="pw12345!")
        self.trainer.groups.add(Group.objects.get_or_create(name="Trainer")[0])
        self.exam, self.qs = make_exam(self.trainer)
        self.client_a = Client()

    def enter(self, client=None, **extra):
        data = {"name": "Alice Mutesi", "reg_no": "nit-001", "password": PASSWORD}
        data.update(extra)
        return (client or self.client_a).post(reverse("assessments:entry", args=[self.exam.public_id]), data)

    def open_page(self, client=None, **extra):
        """Enter + load the exam page. Returns (response, config)."""
        client = client or self.client_a
        r = self.enter(client, **extra)
        self.assertEqual(r.status_code, 302, getattr(r, "content", b"")[:300])
        page = client.get(r["Location"])
        self.assertEqual(page.status_code, 200)
        return page, page.context["config"]

    def call(self, client, cfg, action, **body):
        payload = {"tab": cfg["tabToken"], "device": cfg["deviceToken"], **body}
        return client.post(cfg["api"][action], json.dumps(payload), content_type="application/json")

    def attempt(self):
        return Attempt.objects.get(exam=self.exam)


class AccessTests(ExamTestBase):
    def test_wrong_password_is_refused_and_creates_nothing(self):
        r = self.enter(password="nope")
        self.assertEqual(r.status_code, 403)
        self.assertFalse(Attempt.objects.exists())

    def test_password_and_answer_key_never_reach_the_browser(self):
        entry = self.client_a.get(reverse("assessments:entry", args=[self.exam.public_id]))
        page, cfg = self.open_page()
        html = page.content.decode()
        for blob in (entry.content.decode(), html):
            self.assertNotIn(PASSWORD, blob)
            self.assertNotIn(SECRET_FILL, blob)
            self.assertNotIn("alt answer", blob)
            self.assertNotIn(self.exam.password_hash, blob)
        self.assertNotIn('"correct"', html)
        self.assertNotIn('"pairs"', html)
        self.assertNotIn("accepted", html)
        self.assertNotIn("Mentions leases", html)
        match = next(q for q in cfg["questions"] if q["section"] == "match")
        ids = {r["id"] for r in match["right"]}
        self.assertEqual(len(ids), 3)
        self.assertTrue(all(len(i) == 8 and i not in ("0", "1", "2") for i in ids))

    def test_password_throttle(self):
        for _ in range(services.PASSWORD_MAX_FAILURES):
            self.enter(password="bad")
        self.assertEqual(self.enter(password=PASSWORD).status_code, 429)

    def test_exam_must_be_open_and_on_its_date(self):
        self.exam.is_open = False
        self.exam.save()
        self.assertEqual(self.enter().status_code, 403)
        self.exam.is_open = True
        self.exam.exam_date = timezone.localdate() + timedelta(days=1)
        self.exam.save()
        self.assertEqual(self.enter().status_code, 403)
        self.exam.exam_date = timezone.localdate()
        self.exam.save()
        self.assertEqual(self.enter().status_code, 302)

    def test_refresh_sends_back_to_password_and_two_opens_then_blocked(self):
        page, cfg = self.open_page()                       # open 1
        self.assertEqual(cfg["opens"], 1)
        again = self.client_a.get(reverse("assessments:take", args=[self.exam.public_id]))
        self.assertEqual(again.status_code, 302)           # a refresh finds no ticket
        self.assertIn(reverse("assessments:entry", args=[self.exam.public_id]), again["Location"])

        self.call(self.client_a, cfg, "release")
        page2, cfg2 = self.open_page()                     # open 2
        self.assertEqual(cfg2["opens"], 2)
        self.call(self.client_a, cfg2, "release")

        r = self.enter()                                   # third attempt
        self.assertEqual(r.status_code, 403)
        self.assertContains(r, "already opened", status_code=403)
        self.assertEqual(DeviceOpen.objects.get().opens, 2)

    def test_second_tab_is_blocked_and_costs_no_open(self):
        self.open_page()
        r = self.enter()
        self.assertEqual(r.status_code, 403)
        self.assertContains(r, "another tab", status_code=403)
        self.assertEqual(DeviceOpen.objects.get().opens, 1)

    def test_stale_tab_lock_expires(self):
        self.open_page()
        Attempt.objects.update(last_heartbeat=timezone.now() - timedelta(seconds=services.TAB_STALE_SECONDS + 5))
        self.assertEqual(self.enter().status_code, 302)

    def test_old_tab_is_superseded(self):
        _, cfg_old = self.open_page()
        Attempt.objects.update(last_heartbeat=timezone.now() - timedelta(minutes=5))
        self.open_page()
        self.assertEqual(self.call(self.client_a, cfg_old, "heartbeat").status_code, 409)

    def test_open_count_is_tied_to_device_not_just_cookie(self):
        _, cfg = self.open_page()
        self.call(self.client_a, cfg, "release")
        # Cookies cleared but localStorage copy of the device token survives.
        fresh = Client()
        page, cfg2 = self.open_page(fresh, device_hint=cfg["deviceToken"])
        self.assertEqual(cfg2["opens"], 2)
        self.call(fresh, cfg2, "release")
        again = Client()
        self.assertEqual(self.enter(again, device_hint=cfg["deviceToken"]).status_code, 403)

    def test_clearing_all_browser_data_does_not_give_fresh_opens(self):
        _, cfg = self.open_page()
        self.call(self.client_a, cfg, "release")
        r = self.enter(Client())                           # no cookie, no hint: a "new device"
        self.assertEqual(r.status_code, 403)
        self.assertContains(r, "another computer", status_code=403)

    def test_local_storage_count_can_only_raise_the_count(self):
        _, cfg = self.open_page()
        self.call(self.client_a, cfg, "release")
        r = self.enter(local_opens=2, local_epoch=0)       # server says 1, browser claims 2
        self.assertEqual(r.status_code, 403)

    def test_wrong_tab_token_or_device_is_rejected(self):
        _, cfg = self.open_page()
        bad = {**cfg, "tabToken": "x" * 20}
        self.assertEqual(self.call(self.client_a, bad, "heartbeat").status_code, 409)
        other = Client()
        self.assertEqual(other.post(cfg["api"]["heartbeat"], json.dumps({"tab": cfg["tabToken"], "device": "z" * 30}),
                                    content_type="application/json").status_code, 403)


class ClockAndAnswersTests(ExamTestBase):
    def test_resume_keeps_answers_and_absolute_deadline(self):
        _, cfg = self.open_page()
        self.assertIsNone(cfg["endAtMs"])
        started = self.call(self.client_a, cfg, "start").json()
        end_ms = started["endAtMs"]
        q_mcq, q_fill = self.qs[("mcq", 1)], self.qs[("fill", 1)]
        r = self.call(self.client_a, cfg, "autosave", answers={str(q_mcq.id): 1, str(q_fill.id): "dns"})
        self.assertTrue(r.json()["ok"])
        self.call(self.client_a, cfg, "release")

        _, cfg2 = self.open_page()
        self.assertEqual(cfg2["endAtMs"], end_ms)          # same deadline, not restarted
        self.assertEqual(cfg2["answers"][str(q_mcq.id)], 1)
        self.assertEqual(cfg2["answers"][str(q_fill.id)], "dns")

    def test_autosave_validates_and_can_clear(self):
        _, cfg = self.open_page()
        self.call(self.client_a, cfg, "start")
        q = self.qs[("mcq", 1)]
        self.call(self.client_a, cfg, "autosave", answers={str(q.id): 99, "99999": "x"})
        self.assertEqual(self.attempt().answers, {})
        self.call(self.client_a, cfg, "autosave", answers={str(q.id): 2})
        self.assertEqual(self.attempt().answers, {str(q.id): 2})
        self.call(self.client_a, cfg, "autosave", answers={str(q.id): None})
        self.assertEqual(self.attempt().answers, {})

    def test_time_up_auto_submits(self):
        _, cfg = self.open_page()
        self.call(self.client_a, cfg, "start")
        Attempt.objects.update(end_at=timezone.now() - timedelta(seconds=services.SUBMIT_GRACE_SECONDS + 1))
        r = self.call(self.client_a, cfg, "autosave", answers={})
        self.assertEqual(r.status_code, 409)
        a = self.attempt()
        self.assertTrue(a.is_submitted)
        self.assertEqual(a.submit_reason, "time")

    def test_expired_attempts_are_swept_even_if_the_browser_died(self):
        _, cfg = self.open_page()
        self.call(self.client_a, cfg, "start")
        Attempt.objects.update(end_at=timezone.now() - timedelta(minutes=5))
        services.sweep_expired(self.exam)
        self.assertTrue(self.attempt().is_submitted)


class PenaltyTests(ExamTestBase):
    def started(self):
        _, cfg = self.open_page()
        self.call(self.client_a, cfg, "start")
        return cfg

    def test_one_penalty_per_away_episode(self):
        cfg = self.started()
        r1 = self.call(self.client_a, cfg, "violation", type="fullscreen_exit", episode=1).json()
        self.call(self.client_a, cfg, "violation", type="tab_switch", episode=1)
        r3 = self.call(self.client_a, cfg, "violation", type="window_blur", episode=1).json()
        self.assertEqual((r1["violationCount"], r1["penaltyTotal"]), (1, 2.0))
        self.assertEqual((r3["violationCount"], r3["penaltyTotal"]), (1, 2.0))
        log = list(self.attempt().violations.values_list("vtype", "counted", "marks_deducted"))
        self.assertEqual([(t, c) for t, c, _ in log], [("fullscreen_exit", True), ("tab_switch", False), ("window_blur", False)])
        r4 = self.call(self.client_a, cfg, "violation", type="tab_switch", episode=2).json()
        self.assertEqual((r4["violationCount"], r4["penaltyTotal"]), (2, 4.0))

    def test_clipboard_and_shortcut_penalties_are_configurable(self):
        self.exam.penalties = {"clipboard": 1, "shortcut": 3, "context_menu": 0}
        self.exam.save()
        cfg = self.started()
        self.call(self.client_a, cfg, "violation", type="clipboard")
        self.call(self.client_a, cfg, "violation", type="shortcut")
        r = self.call(self.client_a, cfg, "violation", type="context_menu").json()
        self.assertEqual((r["violationCount"], r["penaltyTotal"]), (2, 4.0))     # zero-penalty type is log-only
        self.assertEqual(self.attempt().violations.count(), 3)

    def test_unknown_violation_types_are_ignored(self):
        cfg = self.started()
        r = self.call(self.client_a, cfg, "violation", type="heartbeat_gap").json()
        self.assertEqual(r["violationCount"], 0)
        self.assertEqual(self.attempt().violations.count(), 0)

    def test_violations_before_the_clock_starts_are_not_accepted(self):
        _, cfg = self.open_page()
        self.assertEqual(self.call(self.client_a, cfg, "violation", type="clipboard").status_code, 409)

    def test_episode_ids_cannot_collide_across_reopens(self):
        cfg = self.started()
        self.call(self.client_a, cfg, "violation", type="fullscreen_exit", episode=1)
        self.call(self.client_a, cfg, "release")
        _, cfg2 = self.open_page()
        r = self.call(self.client_a, cfg2, "violation", type="fullscreen_exit", episode=1).json()
        self.assertEqual(r["violationCount"], 2)           # a new page load is a new episode

    def test_auto_submit_at_max_violations(self):
        self.exam.max_violations = 2
        self.exam.save()
        cfg = self.started()
        self.call(self.client_a, cfg, "violation", type="shortcut")
        r = self.call(self.client_a, cfg, "violation", type="shortcut").json()
        self.assertTrue(r["autoSubmitted"])
        a = self.attempt()
        self.assertEqual((a.status, a.submit_reason), ("submitted", "violations"))
        self.assertEqual(self.call(self.client_a, cfg, "autosave", answers={}).status_code, 409)

    def test_score_never_below_zero(self):
        self.exam.penalties = {"shortcut": 50}
        self.exam.max_violations = 0
        self.exam.save()
        cfg = self.started()
        self.call(self.client_a, cfg, "violation", type="shortcut")
        self.call(self.client_a, cfg, "submit", answers={})
        self.assertEqual(self.attempt().final_score, Decimal("0.00"))
        self.assertEqual(self.attempt().penalty_total, Decimal("50.00"))

    def test_heartbeat_gap_is_logged_but_not_penalised(self):
        cfg = self.started()
        Attempt.objects.update(last_heartbeat=timezone.now() - timedelta(seconds=services.HEARTBEAT_GAP_SECONDS + 30))
        self.call(self.client_a, cfg, "heartbeat")
        a = self.attempt()
        self.assertEqual(a.violation_count, 0)
        v = a.violations.get()
        self.assertEqual((v.vtype, v.counted), ("heartbeat_gap", False))


class MarkingAndTrainerTests(ExamTestBase):
    def submit_all_correct(self):
        _, cfg = self.open_page()
        self.call(self.client_a, cfg, "start")
        match = next(q for q in cfg["questions"] if q["section"] == "match")
        m = self.qs[("match", 1)]
        answers = {
            str(self.qs[("mcq", 1)].id): 1, str(self.qs[("mcq", 2)].id): 1,
            str(self.qs[("fill", 1)].id): "  " + SECRET_FILL.upper() + " ",
            str(m.id): {"0": right_id(m.id, 0), "1": right_id(m.id, 2)},          # one right, one wrong
            str(self.qs[("open", 1)].id): "Hands out IP addresses automatically.",
        }
        r = self.call(self.client_a, cfg, "submit", answers=answers).json()
        self.assertTrue(r["submitted"])
        return cfg

    def test_objective_marking_and_open_pending(self):
        self.submit_all_correct()
        a = self.attempt()
        # mcq 2+2, fill 2 (case/space insensitive), match 1 of 2 pairs = 1  -> 7
        self.assertEqual(a.objective_score, Decimal("7.00"))
        self.assertFalse(a.marking_complete)
        self.assertEqual(a.final_score, Decimal("7.00"))

    def test_trainer_marks_open_question_and_final_updates(self):
        self.submit_all_correct()
        a = self.attempt()
        Attempt.objects.filter(pk=a.pk).update(penalty_total=Decimal("2"))
        c = Client(); c.force_login(self.trainer)
        oq = self.qs[("open", 1)]
        url = reverse("assessments:attempt_mark", args=[self.exam.pk, a.pk])
        c.post(url, {f"mark_{oq.id}": "9"})                     # above the 4-mark maximum: rejected
        self.assertEqual(self.attempt().open_marks, {})
        c.post(url, {f"mark_{oq.id}": "3.5", "trainer_comment": "ok"})
        a = self.attempt()
        self.assertTrue(a.marking_complete)
        self.assertEqual(a.final_score, Decimal("8.50"))        # 7 + 3.5 - 2

    def test_candidate_result_and_answer_sheet(self):
        cfg = self.submit_all_correct()
        r = self.client_a.get(cfg["resultUrl"])
        self.assertContains(r, "Assessment submitted")
        self.assertContains(r, "Provisional")
        sheet = self.client_a.get(reverse("assessments:answer_sheet", args=[self.attempt().access_key]))
        self.assertEqual(sheet.status_code, 200)
        if sheet["Content-Type"] == "application/pdf":
            self.assertIn(".pdf", sheet["Content-Disposition"])
        else:                                                   # no PDF engine: shown on screen, never an HTML download
            self.assertFalse(sheet.has_header("Content-Disposition"))
            body = sheet.content.decode()
            self.assertIn("Hands out IP addresses", body)
            self.assertNotIn("Mentions leases", body)           # marking guide never in candidate output
        stranger = Client()
        self.assertEqual(stranger.get(cfg["resultUrl"]).status_code, 404)

    def test_hidden_scores_when_not_released(self):
        self.exam.show_results = False
        self.exam.save()
        cfg = self.submit_all_correct()
        self.assertNotContains(self.client_a.get(cfg["resultUrl"]), "Provisional")

    def test_reset_opens_after_crash_keeps_answers_and_clock(self):
        _, cfg = self.open_page()
        end_ms = self.call(self.client_a, cfg, "start").json()["endAtMs"]
        q = self.qs[("mcq", 1)]
        self.call(self.client_a, cfg, "autosave", answers={str(q.id): 2})
        self.call(self.client_a, cfg, "release")
        _, cfg2 = self.open_page()
        self.call(self.client_a, cfg2, "release")
        self.assertEqual(self.enter().status_code, 403)         # out of opens

        c = Client(); c.force_login(self.trainer)
        a = self.attempt()
        c.post(reverse("assessments:attempt_reset", args=[self.exam.pk, a.pk]), {"extra_minutes": "10"})
        # the browser still claims 2 opens, but from before the reset
        page, cfg3 = self.open_page(local_opens=2, local_epoch=0)
        self.assertEqual(cfg3["opens"], 1)
        self.assertEqual(cfg3["answers"][str(q.id)], 2)
        self.assertEqual(cfg3["endAtMs"], end_ms + 10 * 60 * 1000)

    def test_trainer_permissions(self):
        other = User.objects.create_user("trainer2", password="pw12345!")
        from django.contrib.auth.models import Group
        for u in (self.trainer, other):
            g, _ = Group.objects.get_or_create(name="Trainer")
            u.groups.add(g)
        c = Client(); c.force_login(other)
        self.assertEqual(c.get(reverse("assessments:exam_detail", args=[self.exam.pk])).status_code, 403)
        self.assertEqual(c.get(reverse("assessments:exam_results", args=[self.exam.pk])).status_code, 403)
        self.assertEqual(Client().get(reverse("assessments:exam_list")).status_code, 302)   # sign in first
        owner = Client(); owner.force_login(self.trainer)
        for name in ("exam_list",):
            self.assertEqual(owner.get(reverse(f"assessments:{name}")).status_code, 200)
        self.assertEqual(owner.get(reverse("assessments:exam_detail", args=[self.exam.pk])).status_code, 200)
        self.assertEqual(owner.get(reverse("assessments:exam_results", args=[self.exam.pk])).status_code, 200)
        self.assertEqual(owner.get(reverse("assessments:exam_results", args=[self.exam.pk]) + "?format=csv").status_code, 200)

    def test_staff_pages_render_for_a_submitted_attempt(self):
        self.submit_all_correct()
        a = self.attempt()
        c = Client(); c.force_login(self.trainer)
        r = c.get(reverse("assessments:attempt_detail", args=[self.exam.pk, a.pk]))
        self.assertContains(r, "Hands out IP addresses")
        self.assertContains(r, "Mentions leases")                # guide is visible to trainers
        self.assertEqual(c.get(reverse("assessments:question_create", args=[self.exam.pk])).status_code, 200)
        self.assertEqual(c.get(reverse("assessments:exam_create")).status_code, 200)
        self.assertEqual(c.get(reverse("assessments:exam_edit", args=[self.exam.pk])).status_code, 200)
        q = self.qs[("match", 1)]
        self.assertEqual(c.get(reverse("assessments:question_edit", args=[self.exam.pk, q.pk])).status_code, 200)


class AuthoringTests(ExamTestBase):
    def test_create_exam_generates_password_and_question_forms_roundtrip(self):
        c = Client(); c.force_login(self.trainer)
        r = c.post(reverse("assessments:exam_create"), {
            "title": "New one", "duration_minutes": 30, "max_opens": 2, "max_devices": 1, "max_violations": 3,
            "marks_mcq": "5", "marks_fill": "0", "marks_open": "0", "marks_match": "0",
            **{f"pen_{k}": v for k, v in {"fullscreen_exit": 2, "tab_switch": 2, "window_blur": 2, "extra_display": 2,
                                           "clipboard": 1, "shortcut": 2, "context_menu": 0}.items()}})
        self.assertEqual(r.status_code, 302, r.content[:500])
        exam = Exam.objects.get(title="New one")
        self.assertTrue(exam.has_password)
        self.assertEqual(exam.penalties["clipboard"], 1.0)
        qurl = reverse("assessments:question_create", args=[exam.pk])
        self.assertEqual(c.post(qurl, {"section": "mcq", "text": "Pick", "weight": 1, "order": 1,
                                       "mcq_options": "a\nb\nc", "mcq_correct": 2}).status_code, 302)
        q = exam.questions.get()
        self.assertEqual(q.payload["options"], ["a", "b", "c"])
        self.assertEqual(q.key.data["correct"], 1)
        bad = c.post(qurl, {"section": "match", "text": "m", "weight": 1, "order": 2, "match_pairs": "only one => pair"})
        self.assertEqual(bad.status_code, 200)                   # needs at least 2 pairs


class ShowAnswersTests(ExamTestBase):
    """Answers reach a candidate only after submitting AND only if the trainer enabled it."""

    def submit_mixed(self):
        _, cfg = self.open_page()
        self.call(self.client_a, cfg, "start")
        m = self.qs[("match", 1)]
        answers = {
            str(self.qs[("mcq", 1)].id): 1,                  # correct
            str(self.qs[("mcq", 2)].id): 0,                  # wrong (correct is 1)
            str(self.qs[("fill", 1)].id): "wrong thing",
            str(m.id): {"0": right_id(m.id, 0), "1": right_id(m.id, 2)},   # 1 of 2 pairs right
            str(self.qs[("open", 1)].id): "Hands out addresses.",
        }
        self.call(self.client_a, cfg, "submit", answers=answers)
        return cfg

    def review_url(self):
        return reverse("assessments:answer_review", args=[self.attempt().access_key])

    def test_hidden_by_default_and_nothing_leaks(self):
        cfg = self.submit_mixed()
        result = self.client_a.get(cfg["resultUrl"])
        self.assertNotContains(result, "Review the answers")
        r = self.client_a.get(self.review_url())
        self.assertEqual(r.status_code, 403)
        for secret in (SECRET_FILL, "Mentions leases", "Connects networks"):
            self.assertNotIn(secret, r.content.decode())

    def test_enabled_shows_correct_answers_per_question(self):
        cfg = self.submit_mixed()
        self.exam.show_answers = True
        self.exam.save()
        self.assertContains(self.client_a.get(cfg["resultUrl"]), "Review the answers")
        r = self.client_a.get(self.review_url())
        self.assertEqual(r.status_code, 200)
        rows = r.context["rows"]
        self.assertEqual([x["status"] for x in rows], ["correct", "wrong", "wrong", "pending", "partial"])
        mcq2 = rows[1]
        self.assertEqual([(o["chosen"], o["correct"]) for o in mcq2["options"]], [(True, False), (False, True), (False, False)])
        self.assertEqual(rows[2]["accepted"], [SECRET_FILL, "alt answer"])
        self.assertEqual([p["ok"] for p in rows[4]["pairs"]], [True, False])
        self.assertEqual(rows[4]["pairs"][1]["correct"], "Connects hosts")
        self.assertEqual(rows[3]["guide"], "Mentions leases and automatic addressing.")
        self.assertEqual(rows[4]["earned"], Decimal("1.00"))
        self.assertContains(r, SECRET_FILL)

    def test_open_marks_appear_once_the_trainer_has_marked(self):
        self.submit_mixed()
        self.exam.show_answers = True
        self.exam.save()
        a = self.attempt()
        oq = self.qs[("open", 1)]
        Attempt.objects.filter(pk=a.pk).update(open_marks={str(oq.id): "3.5"})
        rows = self.client_a.get(self.review_url()).context["rows"]
        self.assertEqual((rows[3]["status"], rows[3]["earned"]), ("marked", Decimal("3.5")))

    def test_marks_hidden_when_scores_are_not_released(self):
        self.submit_mixed()
        self.exam.show_answers = True
        self.exam.show_results = False
        self.exam.save()
        r = self.client_a.get(self.review_url())
        self.assertFalse(r.context["show_marks"])
        self.assertNotContains(r, "/ 2")

    def test_must_be_submitted_and_must_be_that_candidate(self):
        self.exam.show_answers = True
        self.exam.save()
        _, cfg = self.open_page()                                  # still in progress
        key = self.attempt().access_key
        self.assertEqual(self.client_a.get(reverse("assessments:answer_review", args=[key])).status_code, 404)
        self.call(self.client_a, cfg, "submit", answers={})
        self.assertEqual(self.client_a.get(reverse("assessments:answer_review", args=[key])).status_code, 200)
        self.assertEqual(Client().get(reverse("assessments:answer_review", args=[key])).status_code, 404)

    def test_turning_it_off_hides_it_again_and_exam_page_still_has_no_key(self):
        cfg = self.submit_mixed()
        self.exam.show_answers = True
        self.exam.save()
        self.assertEqual(self.client_a.get(self.review_url()).status_code, 200)
        self.exam.show_answers = False
        self.exam.save()
        self.assertEqual(self.client_a.get(self.review_url()).status_code, 403)
        # a second candidate loading the live exam page never receives the key, whatever the setting
        self.exam.show_answers = True
        self.exam.save()
        other = Client()
        resp = other.post(reverse("assessments:entry", args=[self.exam.public_id]),
                          {"name": "Bob", "reg_no": "NIT-777", "password": PASSWORD})
        html = other.get(resp["Location"]).content.decode()
        self.assertNotIn(SECRET_FILL, html)
        self.assertNotIn("Mentions leases", html)

    def test_trainer_toggle_and_in_progress_warning(self):
        c = Client(); c.force_login(self.trainer)
        self.open_page()                                           # one candidate still in progress
        r = c.post(reverse("assessments:exam_toggle_answers", args=[self.exam.pk]), follow=True)
        self.exam.refresh_from_db()
        self.assertTrue(self.exam.show_answers)
        self.assertContains(r, "still in progress")
        c.post(reverse("assessments:exam_toggle_answers", args=[self.exam.pk]))
        self.exam.refresh_from_db()
        self.assertFalse(self.exam.show_answers)
        other = User.objects.create_user("trainer9", password="pw12345!")
        from django.contrib.auth.models import Group
        other.groups.add(Group.objects.get(name="Trainer"))
        oc = Client(); oc.force_login(other)
        self.assertEqual(oc.post(reverse("assessments:exam_toggle_answers", args=[self.exam.pk])).status_code, 403)
        self.assertEqual(Client().post(reverse("assessments:exam_toggle_answers", args=[self.exam.pk])).status_code, 302)
