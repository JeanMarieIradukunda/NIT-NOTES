"""Light/dark theme switch, and the richer summary cards on the trainer and administrator dashboards."""

import re
from datetime import timedelta

from django.urls import reverse
from django.utils import timezone

from assessments.tests import make_exam
from core.models import Activity, ModuleNote
from core.tests import BaseCase


def css(path):
    return open(path, encoding="utf-8").read()


class ThemeSwitch(BaseCase):
    def test_every_page_has_the_toggle_and_loads_the_script_in_the_head(self):
        self.client.force_login(self.alice)
        for name in ("core:dashboard", "core:browse", "core:notes_manage", "assessments:exam_list"):
            html = self.client.get(reverse(name)).content.decode()
            self.assertIn('id="themeToggle"', html, name)
            head = html.split("</head>", 1)[0]
            self.assertIn("js/theme.js", head, name)                # in <head>, so there is no flash on load
            self.assertIn('data-bs-theme="dark"', html, name)       # dark stays the default

    def test_visitors_who_are_not_signed_in_get_the_toggle_too(self):
        self.assertIn('id="themeToggle"', self.client.get(reverse("core:dashboard")).content.decode())

    def test_script_remembers_the_choice_and_survives_blocked_storage(self):
        js = css("static/js/theme.js")
        self.assertIn('"nit-theme"', js)
        self.assertEqual(js.count("try {"), 2)                      # both storage calls are guarded
        self.assertIn('data-bs-theme', js)
        self.assertIn("theme-color", js)

    def test_light_palette_overrides_the_same_tokens_as_dark(self):
        sheet = css("static/css/platform.css")
        dark = sheet.split(":root {", 1)[1].split("/* Bootstrap bridge */", 1)[0]
        light = sheet.split('html[data-bs-theme="light"] {', 1)[1].split("}", 1)[0]
        wanted = re.findall(r"(--c-[a-z0-9-]+):", dark)
        missing = [t for t in wanted if t not in ("--c-navy", "--c-navy-2", "--c-danger-solid", "--c-danger-solid-2") and t + ":" not in light]
        self.assertEqual(missing, [], "tokens defined for dark but forgotten in the light theme")
        self.assertIn("color-scheme: light;", light)

    def test_dashboard_tones_have_light_variants(self):
        sheet = css("static/css/dashboard.css")
        self.assertIn('html[data-bs-theme="light"] body.night', sheet)
        self.assertIn("--tone-teal:", sheet.split('html[data-bs-theme="light"] body.night', 1)[1])

    def test_exam_taking_pages_do_not_load_the_switch(self):
        # Candidates sit assessments in their own fixed shell; the theme switch is for staff pages.
        self.assertNotIn("theme.js", open("templates/assessments/shell.html", encoding="utf-8").read())


class SummaryCards(BaseCase):
    def stats(self, user):
        self.client.force_login(user)
        r = self.client.get(reverse("core:dashboard"))
        return {s["label"]: s for s in r.context["stats"]}, r.content.decode()

    def test_trainer_cards_open_the_filtered_list_and_link_drafts(self):
        self.make_note(self.alice)
        self.make_note(self.alice, published=False)
        stats, html = self.stats(self.alice)
        notes = stats["Notes published"]
        self.assertEqual(notes["url"], reverse("core:notes_manage") + "?status=published")
        self.assertEqual(notes["sub_url"], reverse("core:notes_manage") + "?status=draft")
        self.assertIn(f'href="{notes["sub_url"]}"', html)
        self.assertEqual(stats["Activities published"]["sub_url"], "")          # no drafts: plain text, no link

    def test_the_two_links_are_siblings_never_nested_anchors(self):
        self.make_note(self.alice, published=False)
        _, html = self.stats(self.alice)
        card = html.split('class="nd-stat is-blue"', 1)[1].split("</div>", 1)[0]
        self.assertEqual(card.count("<a "), card.count("</a>"))
        self.assertNotRegex(card, r"<a [^>]*>(?:(?!</a>).)*<a ", "anchors must not be nested")

    def test_trend_counts_this_weeks_new_published_notes(self):
        self.make_note(self.alice)
        old = self.make_note(self.alice)
        ModuleNote.objects.filter(pk=old.pk).update(published_at=timezone.now() - timedelta(days=20))
        stats, html = self.stats(self.alice)
        self.assertEqual(stats["Notes published"]["trend"]["n"], 1)
        self.assertEqual(stats["Notes published"]["trend"]["text"], "+1 new this week")
        self.assertIn("nd-trend is-up", html)

    def test_quiet_week_shows_a_neutral_chip_not_a_zero(self):
        stats, html = self.stats(self.alice)
        t = stats["Activities published"]["trend"]
        self.assertEqual((t["dir"], t["n"], t["text"]), ("flat", 0, "nothing new this week"))
        self.assertIn("nd-trend is-flat", html)

    def test_new_assessments_are_counted_for_the_trainer(self):
        make_exam(self.alice)
        stats, _ = self.stats(self.alice)
        self.assertEqual(stats["Assessments open"]["trend"]["n"], 1)
        stats, _ = self.stats(self.bob)                                          # someone else's exam is not mine
        self.assertEqual(stats["Assessments open"]["trend"]["n"], 0)

    def test_admin_cards_have_trends_except_modules_which_have_no_dates(self):
        make_exam(self.alice)
        stats, _ = self.stats(self.admin)
        self.assertEqual(stats["Assessments open"]["trend"]["n"], 1)
        self.assertEqual(stats["Trainers"]["trend"]["n"], 2)                     # alice and bob joined just now
        self.assertIsNone(stats["Modules"]["trend"])
        self.assertEqual(stats["Published content"]["url"], reverse("core:notes_manage") + "?status=published")

    def test_filtered_links_really_filter(self):
        pub, draft = self.make_note(self.alice), self.make_note(self.alice, published=False)
        self.client.force_login(self.alice)
        r = self.client.get(reverse("core:notes_manage"), {"status": "draft"})
        self.assertEqual({n.pk for n in r.context["page"]}, {draft.pk})
        r = self.client.get(reverse("core:notes_manage"), {"status": "published"})
        self.assertEqual({n.pk for n in r.context["page"]}, {pub.pk})
