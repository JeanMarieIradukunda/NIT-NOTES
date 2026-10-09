"""Saved classes, publishing an assessment to a class, and giving a candidate another chance."""

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import Client
from django.urls import reverse

from . import services
from .models import Attempt, ClassGroup, ClassMember
from .tests import ExamTestBase

User = get_user_model()
LIST = "NIT-001, Alice Mutesi\nNIT-002, Jean Bosco Habimana\nNIT-003, Grace Uwase"


class ClassTests(ExamTestBase):
    def setUp(self):
        super().setUp()
        self.staff = Client()
        self.staff.login(username="trainer1", password="pw12345!")

    def make_class(self, name="Year 2 Networking", paste=LIST):
        return self.staff.post(reverse("assessments:class_list"), {"name": name, "paste": paste})

    def test_create_class_from_pasted_list(self):
        r = self.make_class()
        group = ClassGroup.objects.get(name="Year 2 Networking")
        self.assertRedirects(r, reverse("assessments:class_detail", args=[group.pk]))
        self.assertEqual(group.members.count(), 3)
        self.assertEqual(group.created_by, self.trainer)
        self.assertEqual(self.staff.get(reverse("assessments:class_list")).status_code, 200)
        self.assertEqual(self.staff.get(reverse("assessments:class_detail", args=[group.pk])).status_code, 200)

    def test_class_needs_name_and_candidates(self):
        self.make_class(name="")
        self.make_class(name="Empty", paste="")
        self.assertFalse(ClassGroup.objects.exists())

    def test_add_more_and_remove_members(self):
        self.make_class()
        group = ClassGroup.objects.get()
        self.staff.post(reverse("assessments:class_detail", args=[group.pk]), {"paste": "NIT-004, Eric N", "mode": "add"})
        self.assertEqual(group.members.count(), 4)
        self.staff.post(reverse("assessments:class_detail", args=[group.pk]), {"paste": "NIT-009, Only One", "mode": "replace"})
        self.assertEqual(list(group.members.values_list("reg_no", flat=True)), ["NIT-009"])
        m = group.members.get()
        self.staff.post(reverse("assessments:class_member_delete", args=[group.pk, m.pk]))
        self.assertEqual(group.members.count(), 0)

    def test_publish_exam_to_class_restricts_entry(self):
        self.make_class()
        group = ClassGroup.objects.get()
        self.staff.post(reverse("assessments:roster_assign", args=[self.exam.pk]), {"classes": [group.pk], "mode": "add"})
        self.assertEqual(self.exam.roster.count(), 3)
        self.assertEqual(self.enter(reg_no="NIT-777").status_code, 403)       # not in the class
        self.assertEqual(self.enter(reg_no="nit-001").status_code, 302)       # in the class
        self.assertEqual(self.staff.get(reverse("assessments:roster", args=[self.exam.pk])).status_code, 200)

    def test_assign_requires_a_class_and_replace_works(self):
        self.staff.post(reverse("assessments:roster_assign", args=[self.exam.pk]), {"mode": "add"})
        self.assertEqual(self.exam.roster.count(), 0)
        self.make_class()
        self.make_class(name="Other", paste="NIT-050, Zed")
        a, b = ClassGroup.objects.get(name="Year 2 Networking"), ClassGroup.objects.get(name="Other")
        self.staff.post(reverse("assessments:roster_assign", args=[self.exam.pk]), {"classes": [a.pk], "mode": "add"})
        self.staff.post(reverse("assessments:roster_assign", args=[self.exam.pk]), {"classes": [b.pk], "mode": "replace"})
        self.assertEqual(list(self.exam.roster.values_list("reg_no", flat=True)), ["NIT-050"])

    def test_saving_roster_can_also_save_it_as_a_class(self):
        self.staff.post(reverse("assessments:roster", args=[self.exam.pk]), {"paste": LIST, "mode": "add", "save_as": "Saved from roster"})
        self.assertEqual(self.exam.roster.count(), 3)
        self.assertEqual(ClassGroup.objects.get(name="Saved from roster").members.count(), 3)

    def test_trainers_cannot_see_or_use_each_others_classes_but_admin_can(self):
        self.make_class(name="Secret Cohort")
        group = ClassGroup.objects.get()
        other = User.objects.create_user("trainer2", password="pw12345!")
        other.groups.add(Group.objects.get(name="Trainer"))
        c2 = Client(); c2.login(username="trainer2", password="pw12345!")
        self.assertEqual(c2.get(reverse("assessments:class_detail", args=[group.pk])).status_code, 403)
        self.assertEqual(c2.post(reverse("assessments:class_delete", args=[group.pk])).status_code, 403)
        self.assertNotIn(b"Secret Cohort", c2.get(reverse("assessments:class_list")).content)
        exam2, _ = __import__("assessments.tests", fromlist=["make_exam"]).make_exam(other)
        c2.post(reverse("assessments:roster_assign", args=[exam2.pk]), {"classes": [group.pk], "mode": "add"})
        self.assertEqual(exam2.roster.count(), 0)
        admin = User.objects.create_superuser("boss", password="pw12345!")
        c3 = Client(); c3.login(username="boss", password="pw12345!")
        self.assertEqual(c3.get(reverse("assessments:class_detail", args=[group.pk])).status_code, 200)
        c3.post(reverse("assessments:class_delete", args=[group.pk]))
        self.assertFalse(ClassGroup.objects.exists())


class RetakeTests(ExamTestBase):
    def setUp(self):
        super().setUp()
        self.staff = Client()
        self.staff.login(username="trainer1", password="pw12345!")

    def submit_first_attempt(self):
        page, cfg = self.open_page()
        self.call(self.client_a, cfg, "start")
        self.call(self.client_a, cfg, "violation", type="clipboard", episode="p-1")
        self.assertTrue(self.call(self.client_a, cfg, "submit").json()["submitted"])
        return cfg

    def test_submitted_candidate_is_blocked_then_gets_another_chance(self):
        cfg = self.submit_first_attempt()
        old_key = self.attempt().access_key
        self.assertEqual(self.enter().status_code, 403)                       # already submitted
        a = self.attempt()
        r = self.staff.post(reverse("assessments:attempt_retake", args=[self.exam.pk, a.pk]))
        self.assertEqual(r.status_code, 302)
        a = self.attempt()
        self.assertFalse(a.is_submitted)
        self.assertIsNone(a.end_at)
        self.assertEqual((a.violation_count, a.answers, a.devices.count(), a.violations.count()), (0, {}, 0, 0))
        self.assertIsNone(a.final_score)
        self.assertNotEqual(a.access_key, old_key)
        self.assertEqual(len(a.previous_attempts), 1)
        self.assertEqual(a.previous_attempts[0]["status"], "submitted")
        self.assertEqual(a.previous_attempts[0]["violations"], 1)
        # the old exam tab and result link no longer work
        self.assertEqual(self.client_a.post(cfg["api"]["autosave"], "{}", content_type="application/json").status_code, 404)
        # they can enter again, with a full clock and open count, and finish a second time
        page, cfg2 = self.open_page()
        self.assertEqual(cfg2["opens"], 1)
        self.call(self.client_a, cfg2, "start")
        self.assertIsNotNone(self.attempt().end_at)
        self.assertTrue(self.call(self.client_a, cfg2, "submit").json()["submitted"])
        r = self.staff.get(reverse("assessments:attempt_detail", args=[self.exam.pk, self.attempt().pk]))
        self.assertContains(r, "Earlier chances")

    def test_retake_from_results_page_and_in_progress_attempt(self):
        page, cfg = self.open_page()
        self.call(self.client_a, cfg, "start")
        a = self.attempt()
        self.assertContains(self.staff.get(reverse("assessments:exam_results", args=[self.exam.pk])), "Allow another attempt")
        r = self.staff.post(reverse("assessments:attempt_retake", args=[self.exam.pk, a.pk]), {"next": "results"})
        self.assertRedirects(r, reverse("assessments:exam_results", args=[self.exam.pk]))
        a = self.attempt()
        self.assertIsNone(a.end_at)
        self.assertEqual(a.previous_attempts[0]["status"], "in_progress")

    def test_only_the_owner_or_admin_can_give_another_chance(self):
        self.submit_first_attempt()
        other = User.objects.create_user("trainer2", password="pw12345!")
        other.groups.add(Group.objects.get(name="Trainer"))
        c2 = Client(); c2.login(username="trainer2", password="pw12345!")
        r = c2.post(reverse("assessments:attempt_retake", args=[self.exam.pk, self.attempt().pk]))
        self.assertEqual(r.status_code, 403)
        self.assertTrue(self.attempt().is_submitted)
        self.assertEqual(self.staff.get(reverse("assessments:attempt_retake", args=[self.exam.pk, self.attempt().pk])).status_code, 405)


class ClassEditAndBulkRetakeTests(ExamTestBase):
    def setUp(self):
        super().setUp()
        self.staff = Client()
        self.staff.login(username="trainer1", password="pw12345!")

    def test_edit_member_name_and_number(self):
        self.staff.post(reverse("assessments:class_list"), {"name": "Edit me", "paste": LIST})
        group = ClassGroup.objects.get()
        m = group.members.get(reg_no="NIT-001")
        url = reverse("assessments:class_member_edit", args=[group.pk, m.pk])
        self.assertContains(self.staff.get(url), "Alice Mutesi")
        r = self.staff.post(url, {"name": "  Alice   Mutesi-Kamau ", "reg_no": " nit 101 "})
        self.assertRedirects(r, reverse("assessments:class_detail", args=[group.pk]))
        m.refresh_from_db()
        self.assertEqual((m.name, m.reg_no, m.reg_no_display), ("Alice Mutesi-Kamau", "NIT101", "nit 101"))

    def test_edit_rejects_blank_and_duplicate_numbers(self):
        self.staff.post(reverse("assessments:class_list"), {"name": "Dup", "paste": LIST})
        group = ClassGroup.objects.get()
        m = group.members.get(reg_no="NIT-001")
        url = reverse("assessments:class_member_edit", args=[group.pk, m.pk])
        self.assertEqual(self.staff.post(url, {"name": "X", "reg_no": ""}).status_code, 400)
        r = self.staff.post(url, {"name": "X", "reg_no": "nit-002"})
        self.assertEqual(r.status_code, 400)
        self.assertContains(r, "already has the number", status_code=400)
        m.refresh_from_db()
        self.assertEqual(m.reg_no, "NIT-001")

    def test_edit_is_owner_only(self):
        self.staff.post(reverse("assessments:class_list"), {"name": "Mine", "paste": LIST})
        group = ClassGroup.objects.get()
        m = group.members.first()
        other = User.objects.create_user("trainer2", password="pw12345!")
        other.groups.add(Group.objects.get(name="Trainer"))
        c2 = Client(); c2.login(username="trainer2", password="pw12345!")
        url = reverse("assessments:class_member_edit", args=[group.pk, m.pk])
        self.assertEqual(c2.get(url).status_code, 403)
        self.assertEqual(c2.post(url, {"name": "Hacked", "reg_no": "H-1"}).status_code, 403)
        m.refresh_from_db()
        self.assertNotEqual(m.name, "Hacked")
        self.assertEqual(self.staff.get(reverse("assessments:class_detail", args=[group.pk])).status_code, 200)

    def _two_candidates(self):
        """Alice submits; Bob starts but stays in progress; Carol only opens the entry page."""
        out = {}
        for reg, name in (("nit-001", "Alice"), ("nit-002", "Bob")):
            c = Client()
            page, cfg = self.open_page(c, name=name, reg_no=reg)
            self.call(c, cfg, "start")
            out[reg] = (c, cfg)
        self.call(*out["nit-001"], "submit")
        c3 = Client()
        self.open_page(c3, name="Carol", reg_no="nit-003")
        return out

    def test_whole_class_retake_submitted_only(self):
        self._two_candidates()
        r = self.staff.post(reverse("assessments:exam_retake_all", args=[self.exam.pk]), {"scope": "submitted"})
        self.assertRedirects(r, reverse("assessments:exam_results", args=[self.exam.pk]))
        by = {a.reg_no: a for a in Attempt.objects.filter(exam=self.exam)}
        self.assertEqual(len(by["NIT-001"].previous_attempts), 1)
        self.assertIsNone(by["NIT-001"].end_at)
        self.assertEqual(by["NIT-002"].previous_attempts, [])           # still in progress: untouched
        self.assertIsNotNone(by["NIT-002"].end_at)
        self.assertEqual(by["NIT-003"].previous_attempts, [])

    def test_whole_class_retake_everyone_skips_those_who_never_started(self):
        self._two_candidates()
        self.staff.post(reverse("assessments:exam_retake_all", args=[self.exam.pk]), {"scope": "everyone"})
        by = {a.reg_no: a for a in Attempt.objects.filter(exam=self.exam)}
        self.assertEqual(len(by["NIT-001"].previous_attempts), 1)
        self.assertEqual(len(by["NIT-002"].previous_attempts), 1)
        self.assertIsNone(by["NIT-002"].end_at)
        self.assertEqual(by["NIT-003"].previous_attempts, [])           # never started: nothing to reset
        self.assertEqual(self.enter(name="Alice", reg_no="nit-001").status_code, 302)   # can re-enter

    def test_whole_class_retake_page_button_permissions_and_empty(self):
        self.assertNotContains(self.staff.get(reverse("assessments:exam_results", args=[self.exam.pk])), "whole class")
        self._two_candidates()
        self.assertContains(self.staff.get(reverse("assessments:exam_results", args=[self.exam.pk])), "Another chance for the whole class")
        other = User.objects.create_user("trainer2", password="pw12345!")
        other.groups.add(Group.objects.get(name="Trainer"))
        c2 = Client(); c2.login(username="trainer2", password="pw12345!")
        url = reverse("assessments:exam_retake_all", args=[self.exam.pk])
        self.assertEqual(c2.post(url, {"scope": "everyone"}).status_code, 403)
        self.assertEqual(self.staff.get(url).status_code, 405)
        self.assertTrue(all(a.previous_attempts == [] for a in Attempt.objects.filter(exam=self.exam)))
        # nobody submitted in a fresh exam -> friendly message, no error
        from .tests import make_exam
        exam2, _ = make_exam(self.trainer)
        r = self.staff.post(reverse("assessments:exam_retake_all", args=[exam2.pk]), {"scope": "submitted"}, follow=True)
        self.assertContains(r, "nobody has submitted yet")
