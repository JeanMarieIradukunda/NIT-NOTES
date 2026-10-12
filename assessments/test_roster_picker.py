"""Pick individual students from a saved class and add them to an assessment's class list."""

from django.urls import reverse

from assessments.models import ClassGroup, ClassMember, RosterEntry, normalise_reg_no
from assessments.tests import make_exam
from core.tests import BaseCase


def member(group, number, name):
    return ClassMember.objects.create(group=group, reg_no=normalise_reg_no(number), reg_no_display=number, name=name)


class PickStudentsFromAClass(BaseCase):
    def setUp(self):
        super().setUp()
        self.exam, _ = make_exam(self.alice)
        self.url = reverse("assessments:roster", args=[self.exam.pk])
        self.add_url = reverse("assessments:roster_add_members", args=[self.exam.pk])
        self.y2 = ClassGroup.objects.create(name="Year 2 Networking", created_by=self.alice)
        self.y3 = ClassGroup.objects.create(name="Year 3 Databases", created_by=self.alice)
        self.ann = member(self.y2, "NIT-001", "Ann Mutesi")
        self.ben = member(self.y2, "NIT-002", "Ben Habimana")
        self.cleo = member(self.y2, "NIT-003", "Cleo Uwase")
        self.dan = member(self.y3, "NIT-100", "Dan Kamau")
        self.bobs_class = ClassGroup.objects.create(name="Bob's class", created_by=self.bob)
        self.eve = member(self.bobs_class, "NIT-900", "Eve Not-Yours")
        self.client.force_login(self.alice)

    def listed(self):
        return set(self.exam.roster.values_list("reg_no", flat=True))

    # ---- the picker on the page
    def test_page_offers_a_class_chooser_and_lists_that_classs_students(self):
        r = self.client.get(self.url)
        self.assertContains(r, "Pick students from a class")
        self.assertContains(r, "Year 2 Networking")
        self.assertNotContains(r, "Bob&#x27;s class")                   # other trainers' classes are not offered
        self.assertIsNone(r.context["picker_class"])                    # two classes: the trainer chooses
        r = self.client.get(self.url, {"from": self.y2.pk})
        self.assertEqual(r.context["picker_class"], self.y2)
        for m in (self.ann, self.ben, self.cleo):
            self.assertContains(r, f'name="members" value="{m.pk}"')
        self.assertNotContains(r, f'value="{self.dan.pk}"')             # a different class
        self.assertEqual(r.context["picker_new"], 3)

    def test_a_single_class_is_chosen_automatically(self):
        self.y3.delete()
        r = self.client.get(self.url)
        self.assertEqual(r.context["picker_class"], self.y2)

    def test_students_already_on_the_list_are_shown_but_cannot_be_ticked_again(self):
        RosterEntry.objects.create(exam=self.exam, reg_no=self.ann.reg_no, name="Ann Mutesi")
        r = self.client.get(self.url, {"from": self.y2.pk})
        by_id = {row["m"].pk: row["on_list"] for row in r.context["picker"]}
        self.assertEqual(by_id, {self.ann.pk: True, self.ben.pk: False, self.cleo.pk: False})
        self.assertEqual(r.context["picker_new"], 2)
        self.assertContains(r, "On the list")

    def test_someone_elses_class_id_shows_no_picker(self):
        r = self.client.get(self.url, {"from": self.bobs_class.pk})
        self.assertIsNone(r.context["picker_class"])
        self.assertNotContains(r, "Eve Not-Yours")

    def test_everyone_listed_says_so(self):
        for m in (self.ann, self.ben, self.cleo):
            RosterEntry.objects.create(exam=self.exam, reg_no=m.reg_no, name=m.name)
        self.assertContains(self.client.get(self.url, {"from": self.y2.pk}), "Everyone in this class is already on the list")

    def test_empty_class_points_to_adding_students(self):
        empty = ClassGroup.objects.create(name="Empty", created_by=self.alice)
        r = self.client.get(self.url, {"from": empty.pk})
        self.assertContains(r, "This class has no students yet")
        self.assertContains(r, reverse("assessments:class_detail", args=[empty.pk]))

    def test_no_classes_at_all_explains_how_to_start(self):
        ClassGroup.objects.filter(created_by=self.alice).delete()
        self.assertContains(self.client.get(self.url), "You have no saved classes yet")

    # ---- adding the ticked students
    def test_only_the_ticked_students_are_added_with_their_names_and_numbers(self):
        r = self.client.post(self.add_url, {"members": [self.ann.pk, self.cleo.pk], "from": self.y2.pk}, follow=True)
        self.assertRedirects(r, f"{self.url}?from={self.y2.pk}")
        self.assertEqual(self.listed(), {self.ann.reg_no, self.cleo.reg_no})        # Ben was not ticked
        entry = self.exam.roster.get(reg_no=self.ann.reg_no)
        self.assertEqual((entry.name, entry.reg_no_display), ("Ann Mutesi", "NIT-001"))
        self.assertContains(r, "Added 2 students to the class list.")

    def test_students_can_come_from_several_classes_one_after_another(self):
        self.client.post(self.add_url, {"members": [self.ann.pk], "from": self.y2.pk})
        r = self.client.post(self.add_url, {"members": [self.dan.pk], "from": self.y3.pk}, follow=True)
        self.assertEqual(self.listed(), {self.ann.reg_no, self.dan.reg_no})
        self.assertContains(r, "Added 1 student to the class list.")

    def test_people_already_listed_are_skipped_not_duplicated(self):
        RosterEntry.objects.create(exam=self.exam, reg_no=self.ann.reg_no, name="Ann (typed by hand)")
        r = self.client.post(self.add_url, {"members": [self.ann.pk, self.ben.pk], "from": self.y2.pk}, follow=True)
        self.assertContains(r, "Added 1 student to the class list, 1 was already on it.")
        self.assertEqual(self.exam.roster.filter(reg_no=self.ann.reg_no).count(), 1)
        self.assertEqual(self.exam.roster.get(reg_no=self.ann.reg_no).name, "Ann (typed by hand)")   # untouched

    def test_first_additions_warn_that_entry_is_now_limited(self):
        r = self.client.post(self.add_url, {"members": [self.ann.pk]}, follow=True)
        self.assertContains(r, "Entry is now limited to the students on this list")
        r = self.client.post(self.add_url, {"members": [self.ben.pk]}, follow=True)
        self.assertNotContains(r, "Entry is now limited")                  # only said when the list starts

    def test_ticking_nobody_is_an_error(self):
        r = self.client.post(self.add_url, {"from": self.y2.pk}, follow=True)
        self.assertContains(r, "Tick at least one student to add.")
        self.assertEqual(self.listed(), set())

    def test_students_from_classes_you_do_not_own_are_ignored(self):
        r = self.client.post(self.add_url, {"members": [self.eve.pk], "from": self.bobs_class.pk}, follow=True)
        self.assertContains(r, "Tick at least one student to add.")
        self.assertEqual(self.listed(), set())
        self.client.post(self.add_url, {"members": [self.eve.pk, self.ann.pk]})
        self.assertEqual(self.listed(), {self.ann.reg_no})                  # Ann yes, Eve silently no

    def test_administrator_can_add_from_any_class(self):
        self.client.force_login(self.admin)
        self.client.post(self.add_url, {"members": [self.eve.pk]})
        self.assertEqual(self.listed(), {self.eve.reg_no})

    # ---- who may do it
    def test_other_trainers_students_and_visitors_cannot(self):
        self.client.force_login(self.bob)
        self.assertEqual(self.client.post(self.add_url, {"members": [self.eve.pk]}).status_code, 403)
        self.client.force_login(self.student)
        self.assertEqual(self.client.post(self.add_url, {"members": [self.ann.pk]}).status_code, 403)
        self.client.logout()
        self.assertEqual(self.client.post(self.add_url, {"members": [self.ann.pk]}).status_code, 302)
        self.assertEqual(self.listed(), set())

    def test_only_post(self):
        self.assertEqual(self.client.get(self.add_url).status_code, 405)

    def test_the_whole_class_option_still_works(self):
        r = self.client.post(reverse("assessments:roster_assign", args=[self.exam.pk]),
                             {"classes": [self.y2.pk], "mode": "add"}, follow=True)
        self.assertEqual(self.listed(), {self.ann.reg_no, self.ben.reg_no, self.cleo.reg_no})
