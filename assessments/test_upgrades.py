"""Tests for: class roster, per-candidate shuffling, multi-answer MCQ, entry window, item analysis."""

import json
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import analysis, roster
from .marking import (correct_set, mark_question, option_order, ordered_questions, public_questions,
                      question_max_marks, review_questions, right_id)
from .models import AnswerKey, Attempt, Exam, Question, RosterEntry
from .tests import PASSWORD, PLAIN_STATIC, SECRET_FILL, ExamTestBase, make_exam

User = get_user_model()


class RosterParsingTests(TestCase):
    def test_formats_header_swap_quotes_and_duplicates(self):
        text = 'Candidate number,Full name\nNIT-001, Alice Mutesi\n"NIT-002","Habimana, Jean Bosco"\nJoy Uwase;NIT-003\nNIT-001,Dup Person\n,No Number\n'
        rows, problems = roster.parse_roster(text)
        self.assertEqual([(r["reg_no"], r["name"]) for r in rows],
                         [("NIT-001", "Alice Mutesi"), ("NIT-002", "Habimana, Jean Bosco"), ("NIT-003", "Joy Uwase")])
        self.assertEqual(len(problems), 2)
        self.assertTrue(any("appears twice" in p for p in problems))

    def test_tab_separated_and_number_only(self):
        rows, _ = roster.parse_roster("A 1\tAnna K\nB 2\t\n")
        self.assertEqual([(r["reg_no"], r["name"]) for r in rows], [("A1", "Anna K"), ("B2", "")])


@override_settings(STORAGES=PLAIN_STATIC)
class RosterEntryTests(ExamTestBase):
    def add(self, *rows):
        for reg, name in rows:
            RosterEntry.objects.create(exam=self.exam, reg_no=reg.upper().replace(" ", ""), reg_no_display=reg, name=name)

    def test_unlisted_candidate_is_blocked_after_the_password_check(self):
        self.add(("NIT-001", "Alice Mutesi"))
        wrong_pw = self.enter(reg_no="NIT-999", password="bad")
        self.assertEqual(wrong_pw.status_code, 403)
        self.assertNotContains(wrong_pw, "not on the class list", status_code=403)        # nothing leaks without the password
        r = self.enter(reg_no="NIT-999")
        self.assertContains(r, "not on the class list", status_code=403)
        self.assertFalse(Attempt.objects.exists())

    def test_listed_candidate_gets_the_listed_name_and_typos_cannot_duplicate(self):
        self.add(("NIT-001", "Alice Mutesi"))
        page, cfg = self.open_page(name="Alise", reg_no="nit-001")          # typo'd name, sloppy case
        self.assertEqual(cfg["candidate"]["name"], "Alice Mutesi")
        self.assertEqual(Attempt.objects.count(), 1)
        self.call(self.client_a, cfg, "release")
        self.open_page(name="Someone Else", reg_no=" NIT-001 ")
        self.assertEqual(Attempt.objects.count(), 1)

    def test_name_is_optional_on_a_class_list(self):
        self.add(("NIT-001", "Alice Mutesi"))
        page, cfg = self.open_page(name="")
        self.assertEqual(cfg["candidate"]["name"], "Alice Mutesi")
        self.assertContains(self.client_a.get(reverse("assessments:entry", args=[self.exam.public_id])), "optional")

    def test_number_only_roster_needs_a_typed_name(self):
        self.add(("NIT-002", ""))
        self.assertContains(self.enter(reg_no="NIT-002", name=""), "Enter your full name", status_code=400)
        self.assertEqual(self.enter(reg_no="NIT-002", name="Bob K").status_code, 302)

    def test_no_roster_means_open_entry_as_before(self):
        self.assertEqual(self.enter(reg_no="ANYTHING-1").status_code, 302)

    def test_trainer_upload_replace_delete_and_clear(self):
        c = Client(); c.force_login(self.trainer)
        url = reverse("assessments:roster", args=[self.exam.pk])
        c.post(url, {"paste": "N1, Ann\nN2, Ben", "mode": "add"})
        self.assertEqual(self.exam.roster.count(), 2)
        c.post(url, {"file": SimpleUploadedFile("c.csv", "reg,name\nN2,Benny\nN3,Cy\n".encode()), "mode": "add"})
        self.assertEqual({e.reg_no: e.name for e in self.exam.roster.all()}, {"N1": "Ann", "N2": "Benny", "N3": "Cy"})
        c.post(url, {"paste": "Z9, Zed", "mode": "replace"})
        self.assertEqual([e.reg_no for e in self.exam.roster.all()], ["Z9"])
        c.post(reverse("assessments:roster_delete", args=[self.exam.pk, self.exam.roster.get().pk]))
        self.assertEqual(self.exam.roster.count(), 0)
        c.post(url, {"paste": "N1, Ann"}); c.post(reverse("assessments:roster_clear", args=[self.exam.pk]))
        self.assertEqual(self.exam.roster.count(), 0)
        self.assertEqual(c.get(url).status_code, 200)

    def test_roster_page_shows_status_and_unlisted_started_candidates(self):
        self.enter(reg_no="OLD-1", name="Old Timer")                         # started before any list existed
        self.add(("NEW-1", "New Person"))
        c = Client(); c.force_login(self.trainer)
        page = c.get(reverse("assessments:roster", args=[self.exam.pk]))
        self.assertContains(page, "not on this list")
        self.assertContains(page, "Old Timer")

    def test_other_trainer_cannot_touch_the_class_list(self):
        other = User.objects.create_user("o", password="pw12345!")
        other.groups.add(Group.objects.get(name="Trainer"))
        oc = Client(); oc.force_login(other)
        self.assertEqual(oc.get(reverse("assessments:roster", args=[self.exam.pk])).status_code, 403)
        self.assertEqual(oc.post(reverse("assessments:roster", args=[self.exam.pk]), {"paste": "x, y"}).status_code, 403)


@override_settings(STORAGES=PLAIN_STATIC)
class EntryWindowTests(ExamTestBase):
    def test_before_opening_time_is_refused_for_everyone(self):
        self.exam.opens_at = timezone.now() + timedelta(hours=1)
        self.exam.save()
        self.assertContains(self.enter(), "opens on", status_code=403)

    def test_late_entry_cutoff_blocks_new_starts_but_not_crash_recovery(self):
        self.exam.closes_at = timezone.now() + timedelta(minutes=10)
        self.exam.save()
        _, cfg = self.open_page()
        self.call(self.client_a, cfg, "start")                                # this candidate has started the clock
        self.call(self.client_a, cfg, "release")
        Exam.objects.filter(pk=self.exam.pk).update(closes_at=timezone.now() - timedelta(minutes=1))   # cutoff passes
        again = self.enter()
        self.assertEqual(again.status_code, 302)                              # started candidate may reopen
        late = self.enter(reg_no="LATE-1", name="Late Larry")
        self.assertContains(late, "Entry closed", status_code=403)
        self.assertFalse(Attempt.objects.filter(reg_no="LATE-1").exists())    # no junk attempt row

    def test_candidate_who_opened_but_never_started_the_clock_is_still_late(self):
        self.exam.closes_at = timezone.now() + timedelta(minutes=10)
        self.exam.save()
        _, cfg = self.open_page()
        self.call(self.client_a, cfg, "release")
        Exam.objects.filter(pk=self.exam.pk).update(closes_at=timezone.now() - timedelta(minutes=1))
        self.assertEqual(self.enter().status_code, 403)

    def test_form_validates_window_order(self):
        c = Client(); c.force_login(self.trainer)
        base = {"title": "W", "duration_minutes": 30, "max_opens": 2, "max_devices": 1, "max_violations": 3,
                "marks_mcq": "5", "marks_fill": "0", "marks_open": "0", "marks_match": "0", "multi_scoring": "partial",
                **{f"pen_{k}": 1 for k in ("fullscreen_exit", "tab_switch", "window_blur", "extra_display", "clipboard", "shortcut", "context_menu")}}
        bad = c.post(reverse("assessments:exam_create"), {**base, "opens_at": "2026-10-10T10:00", "closes_at": "2026-10-10T09:00"})
        self.assertEqual(bad.status_code, 200)
        ok = c.post(reverse("assessments:exam_create"), {**base, "opens_at": "2026-10-10T09:00", "closes_at": "2026-10-10T09:15"})
        self.assertEqual(ok.status_code, 302)
        e = Exam.objects.get(title="W")
        self.assertEqual(timezone.localtime(e.closes_at) - timezone.localtime(e.opens_at), timedelta(minutes=15))


@override_settings(STORAGES=PLAIN_STATIC)
class ShuffleTests(ExamTestBase):
    def many(self, n=12):
        for i in range(n):
            q = Question.objects.create(exam=self.exam, section="mcq", order=10 + i, text=f"Extra {i}?",
                                        payload={"options": ["a", "b", "c", "d"]})
            AnswerKey.objects.create(question=q, data={"correct": [0]})

    def order_for(self, reg):
        c = Client()
        _, cfg = self.open_page(c, reg_no=reg)
        return c, cfg, [q["id"] for q in cfg["questions"]]

    def test_off_by_default_everyone_sees_the_same_order(self):
        self.many()
        _, _, a = self.order_for("S1")
        _, _, b = self.order_for("S2")
        self.assertEqual(a, b)

    def test_questions_shuffle_inside_each_section_and_stay_stable(self):
        self.many()
        self.exam.shuffle_questions = True
        self.exam.save()
        c, cfg, a = self.order_for("S1")
        _, _, b = self.order_for("S2")
        self.assertNotEqual(a, b)
        sections = [q["section"] for q in cfg["questions"]]
        self.assertEqual(sections, sorted(sections, key=["mcq", "fill", "open", "match"].index))   # sections stay in order
        self.call(c, cfg, "release")
        _, cfg2 = self.open_page(c, reg_no="S1")                              # reopen: same paper
        self.assertEqual([q["id"] for q in cfg2["questions"]], a)

    def test_layout_is_frozen_when_questions_are_added_later(self):
        self.many(8)
        self.exam.shuffle_questions = True
        self.exam.save()
        c, cfg, a = self.order_for("S1")
        self.call(c, cfg, "release")
        newq = Question.objects.create(exam=self.exam, section="mcq", order=99, text="Late addition?",
                                       payload={"options": ["x", "y"]})
        AnswerKey.objects.create(question=newq, data={"correct": [0]})
        _, cfg2 = self.open_page(c, reg_no="S1")
        ids = [q["id"] for q in cfg2["questions"]]
        self.assertEqual([i for i in ids if i != newq.id], a)                # existing order untouched
        self.assertIn(newq.id, ids)
        self.assertGreater(ids.index(newq.id), max(ids.index(i) for i in a if i in
                           [q.id for q in Question.objects.filter(exam=self.exam, section="mcq")]))   # joins the end of its section

    def test_options_shuffle_with_stable_saved_values_and_correct_marking(self):
        self.exam.shuffle_options = True
        self.exam.save()
        self.many(10)
        seen = set()
        for reg in ("S1", "S2", "S3", "S4", "S5"):
            c, cfg, _ = self.order_for(reg)
            q = next(x for x in cfg["questions"] if x["text"] == "Extra 0?")
            seen.add(tuple(q["optionIdx"]))
            self.assertEqual(sorted(q["optionIdx"]), [0, 1, 2, 3])
            self.assertEqual([q["options"][j] for j in range(4)], ["abcd"[i] for i in q["optionIdx"]])
            # the candidate ticks the displayed option whose saved value is the original index 0
            self.call(c, cfg, "start")
            qid = Question.objects.get(text="Extra 0?").id
            self.call(c, cfg, "submit", answers={str(qid): 0})
            a = Attempt.objects.get(reg_no=reg)
            self.assertGreater(a.objective_score, 0)                       # marked on the original index, not the position
            self.assertEqual(a.answers[str(qid)], 0)
        self.assertGreater(len(seen), 1)

    def test_all_of_the_above_questions_keep_their_order(self):
        self.exam.shuffle_options = True
        self.exam.save()
        q = Question.objects.create(exam=self.exam, section="mcq", order=50, text="Which?",
                                    payload={"options": ["one", "two", "All of the above"]})
        AnswerKey.objects.create(question=q, data={"correct": [2]})
        for pk in range(1, 8):
            fake = Attempt(pk=pk, exam=self.exam)
            self.assertEqual(option_order(self.exam, fake, q), [0, 1, 2])

    def test_review_sheet_and_trainer_view_use_the_candidates_order(self):
        self.many(10)
        self.exam.shuffle_questions = True
        self.exam.show_answers = True
        self.exam.save()
        c, cfg, ids = self.order_for("S1")
        self.call(c, cfg, "start")
        self.call(c, cfg, "submit", answers={})
        a = Attempt.objects.get(reg_no="S1")
        rows = review_questions(self.exam, a)
        shown = [q["text"] for q in cfg["questions"]]
        self.assertEqual([r["text"] for r in rows], shown)
        review = c.get(reverse("assessments:answer_review", args=[a.access_key]))
        self.assertEqual([r["text"] for r in review.context["rows"]], shown)
        t = Client(); t.force_login(self.trainer)
        detail = t.get(reverse("assessments:attempt_detail", args=[self.exam.pk, a.pk]))
        self.assertEqual([r["q"].text for r in detail.context["rows"]], shown)


class MultiAnswerMarkingTests(TestCase):
    def setUp(self):
        owner = User.objects.create_user("m", password="x")
        self.exam = Exam.objects.create(title="M", created_by=owner, marks_mcq=Decimal("4"))
        self.mk = lambda multi, correct, n=4: Question.objects.create(
            exam=self.exam, section="mcq", order=1, text="q", payload={"options": list("abcdefgh")[:n], **({"multi": True} if multi else {})})

    def test_legacy_single_int_key_still_works(self):
        q = self.mk(False, None)
        self.assertEqual(correct_set({"correct": 2}), {2})
        self.assertEqual(mark_question(q, {"correct": 2}, 2, Decimal("4")), Decimal("4"))
        self.assertEqual(mark_question(q, {"correct": 2}, 1, Decimal("4")), Decimal("0"))

    def test_accept_any_of_several_for_single_choice(self):
        q = self.mk(False, None)
        key = {"correct": [0, 2]}
        for pick, want in ((0, "4"), (2, "4"), (1, "0")):
            self.assertEqual(mark_question(q, key, pick, Decimal("4")), Decimal(want))

    def test_select_all_partial_and_all_or_nothing(self):
        q = self.mk(True, None, 5)
        key = {"correct": [0, 2, 3]}
        mx = Decimal("3")
        cases = {  # chosen -> partial marks
            (0, 2, 3): "3", (0, 2): "2", (0,): "1", (0, 2, 3, 1): "2", (0, 1): "0", (1, 4): "0"}
        for chosen, want in cases.items():
            self.assertEqual(mark_question(q, key, list(chosen), mx, "partial"), Decimal(want), chosen)
            self.assertEqual(mark_question(q, key, list(chosen), mx, "all"), Decimal("3") if chosen == (0, 2, 3) else Decimal("0"), chosen)

    def test_select_all_answers_are_cleaned(self):
        from .marking import clean_answer
        q = self.mk(True, None, 4)
        self.assertEqual(clean_answer(q, [3, 1, 1, 9, "x", True]), [1, 3])
        self.assertIsNone(clean_answer(q, []))
        self.assertEqual(clean_answer(q, 2), [2])
        single = self.mk(False, None)
        self.assertIsNone(clean_answer(single, [1, 2]))
        self.assertEqual(clean_answer(single, 3), 3)


@override_settings(STORAGES=PLAIN_STATIC)
class MultiAnswerFlowTests(ExamTestBase):
    def setUp(self):
        super().setUp()
        self.multi = Question.objects.create(exam=self.exam, section="mcq", order=5, text="Pick all primes",
                                             payload={"options": ["2", "4", "5", "9"], "multi": True})
        AnswerKey.objects.create(question=self.multi, data={"correct": [0, 2]})
        self.exam.marks_mcq = Decimal("6")                   # 3 mcq questions, 2 marks each
        self.exam.show_answers = True
        self.exam.save()

    def test_page_flags_multi_without_leaking_the_key_and_autosaves_lists(self):
        page, cfg = self.open_page()
        item = next(q for q in cfg["questions"] if q["id"] == self.multi.id)
        self.assertTrue(item["multi"])
        self.assertNotIn("correct", json.dumps(cfg["questions"]))
        self.call(self.client_a, cfg, "start")
        self.call(self.client_a, cfg, "autosave", answers={str(self.multi.id): [2, 0]})
        self.assertEqual(self.attempt().answers[str(self.multi.id)], [0, 2])
        self.call(self.client_a, cfg, "release")
        _, cfg2 = self.open_page()
        self.assertEqual(cfg2["answers"][str(self.multi.id)], [0, 2])

    def test_partial_marking_review_and_sheet(self):
        _, cfg = self.open_page()
        self.call(self.client_a, cfg, "start")
        self.call(self.client_a, cfg, "submit", answers={str(self.multi.id): [0, 1]})   # 1 right, 1 wrong -> 0
        a = self.attempt()
        self.assertEqual(a.objective_score, Decimal("0.00"))
        row = next(r for r in review_questions(self.exam, a) if r["text"] == "Pick all primes")
        self.assertEqual((row["multi"], row["status"]), (True, "wrong"))
        self.assertEqual([(o["chosen"], o["correct"]) for o in row["options"]],
                         [(True, True), (True, False), (False, True), (False, False)])
        self.assertContains(self.client_a.get(reverse("assessments:answer_sheet", args=[a.access_key])), "2; 4")

    def test_all_or_nothing_setting(self):
        self.exam.multi_scoring = "all"
        self.exam.save()
        _, cfg = self.open_page()
        self.call(self.client_a, cfg, "start")
        self.call(self.client_a, cfg, "submit", answers={str(self.multi.id): [0]})
        self.assertEqual(self.attempt().objective_score, Decimal("0.00"))

    def test_question_form_accepts_several_numbers_and_validates_multi(self):
        c = Client(); c.force_login(self.trainer)
        url = reverse("assessments:question_create", args=[self.exam.pk])
        base = {"section": "mcq", "text": "Pick", "weight": 1, "order": 9, "mcq_options": "a\nb\nc\nd"}
        bad = c.post(url, {**base, "mcq_correct": "1", "mcq_multi": "on"})
        self.assertEqual(bad.status_code, 200)                                 # multi needs 2+
        self.assertEqual(c.post(url, {**base, "mcq_correct": "1, 3", "mcq_multi": "on"}).status_code, 302)
        q = Question.objects.get(text="Pick")
        self.assertEqual((q.key.data["correct"], q.payload.get("multi")), ([0, 2], True))
        self.assertEqual(c.post(url, {**base, "text": "Any", "mcq_correct": "2 and 4"}).status_code, 302)
        q2 = Question.objects.get(text="Any")
        self.assertEqual((q2.key.data["correct"], q2.payload.get("multi")), ([1, 3], None))
        self.assertEqual(c.post(url, {**base, "text": "Out", "mcq_correct": "9"}).status_code, 200)
        edit = c.get(reverse("assessments:question_edit", args=[self.exam.pk, q.pk]))
        self.assertContains(edit, "1, 3")

    def test_importer_multi_letters_and_review_flow(self):
        from . import importer
        from .test_import import docx_bytes, para
        def build(doc):
            doc.add_heading("Section A: Multiple choice", level=1)
            para(doc, "1. Which are fruit?"); para(doc, "A. Apple\nB. Carrot\nC. Mango\nD. Leek"); para(doc, "Answer: A, C")
            para(doc, "2. Pick the capital."); para(doc, "A. Kigali\nB. Paris"); para(doc, "Answer: A")
        d = importer.parse_upload("m.docx", docx_bytes(build))
        q1, q2 = d["questions"]
        self.assertEqual((q1["correct"], q1["multi"]), ([0, 2], True))
        self.assertTrue(any("More than one correct" in w for w in q1["warnings"]))
        self.assertEqual((q2["correct"], q2["multi"]), ([0], False))
        t = Client(); t.force_login(self.trainer)
        r = t.post(reverse("assessments:import_upload", args=[self.exam.pk]), {"file": SimpleUploadedFile("m.docx", docx_bytes(build))})
        page = t.get(r["Location"])
        self.assertContains(page, "Select all that apply")
        # trainer unticks "select all": any one of the two is then accepted
        t.post(r["Location"], {"inc_0": "1", "correct_0": ["0", "2"], "inc_1": "1", "correct_1": "0"})
        got = Question.objects.get(text="Which are fruit?")
        self.assertEqual((got.key.data["correct"], got.payload.get("multi")), ([0, 2], None))
        r2 = t.post(reverse("assessments:import_upload", args=[self.exam.pk]), {"file": SimpleUploadedFile("m.docx", docx_bytes(build))})
        t.post(r2["Location"], {"inc_0": "1", "correct_0": ["0", "2"], "multi_0": "1"})
        self.assertTrue(Question.objects.filter(text="Which are fruit?", payload__multi=True).exists())


@override_settings(STORAGES=PLAIN_STATIC)
class ItemAnalysisTests(ExamTestBase):
    def run_candidate(self, reg, answers):
        c = Client()
        _, cfg = self.open_page(c, reg_no=reg, name=f"Cand {reg}")
        self.call(c, cfg, "start")
        self.call(c, cfg, "submit", answers=answers)

    def test_analysis_numbers_flags_and_distribution(self):
        m1, m2 = self.qs[("mcq", 1)], self.qs[("mcq", 2)]
        f, mt = self.qs[("fill", 1)], self.qs[("match", 1)]
        good = {str(m1.id): 1, str(m2.id): 1, str(f.id): SECRET_FILL,
                str(mt.id): {"0": right_id(mt.id, 0), "1": right_id(mt.id, 1)}}
        for i in range(1, 8):                                               # 7 candidates
            ans = dict(good)
            if i <= 5:
                ans[str(m2.id)] = 2                                         # 5 of 7 pick the same wrong option on mcq 2
            if i <= 3:
                ans[str(f.id)] = "dhcp"
            if i == 7:
                ans.pop(str(m1.id))                                         # one blank
            self.run_candidate(f"R{i}", ans)
        summary, items = analysis.analyse(self.exam)
        self.assertEqual(summary["n"], 7)
        i1, i2, ifill, imatch, iopen = items[0], items[1], items[2], items[4], items[3]
        self.assertEqual(i1["section"], "mcq")
        self.assertEqual((i1["full"], i1["answered"], i1["pct_correct"]), (6, 6, 86))
        self.assertEqual(i1["options"][1]["count"], 6)
        self.assertEqual(i1["blank"], 1)
        self.assertEqual((i2["full"], i2["pct_correct"]), (2, 29))
        self.assertTrue(any("Very hard" in f_[1] for f_ in i2["flags"]))
        self.assertTrue(any("Check the key" in f_[1] for f_ in i2["flags"]))   # a wrong option beat the key
        self.assertEqual(i2["top_wrong"], "443")
        self.assertEqual(ifill["wrong_answers"], [("dhcp", 3)])
        self.assertEqual(imatch["pairs"][0]["pct"], 100)
        self.assertEqual(imatch["pairs"][1]["pct"], 100)
        self.assertEqual(iopen["section"], "open")
        self.assertEqual(sum(b["count"] for b in summary["bins"]), 7)
        self.assertIsNotNone(summary["mean"])

    def test_page_csv_permissions_and_small_groups_not_flagged(self):
        self.run_candidate("R1", {})
        t = Client(); t.force_login(self.trainer)
        page = t.get(reverse("assessments:exam_analysis", args=[self.exam.pk]))
        self.assertContains(page, "fewer than 5 submitted")
        self.assertTrue(all(not it["flags"] for it in page.context["items"]))
        csv = t.get(reverse("assessments:exam_analysis", args=[self.exam.pk]) + "?format=csv")
        self.assertEqual(csv.status_code, 200)
        self.assertIn("Fully correct", csv.content.decode())
        other = User.objects.create_user("zz", password="pw12345!")
        other.groups.add(Group.objects.get(name="Trainer"))
        oc = Client(); oc.force_login(other)
        self.assertEqual(oc.get(reverse("assessments:exam_analysis", args=[self.exam.pk])).status_code, 403)
        self.assertEqual(Client().get(reverse("assessments:exam_analysis", args=[self.exam.pk])).status_code, 302)

    def test_empty_exam_page_renders(self):
        t = Client(); t.force_login(self.trainer)
        self.assertContains(t.get(reverse("assessments:exam_analysis", args=[self.exam.pk])), "No submitted attempts yet")

    def test_discrimination_needs_enough_candidates_and_separates_strong_from_weak(self):
        m1 = self.qs[("mcq", 1)]
        m2 = self.qs[("mcq", 2)]
        for i in range(12):
            strong = i < 6
            ans = {str(m1.id): 1, str(m2.id): 1 if strong else 0}
            if strong:
                ans[str(self.qs[("fill", 1)].id)] = SECRET_FILL
            self.run_candidate(f"D{i}", ans)
        _, items = analysis.analyse(self.exam)
        self.assertEqual(items[1]["disc"], 1.0)           # only strong candidates got mcq 2
        self.assertEqual(items[0]["disc"], 0.0)           # everyone got mcq 1: no separation
