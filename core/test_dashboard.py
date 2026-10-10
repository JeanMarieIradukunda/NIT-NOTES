"""Tests for the three dashboards (landing page): student, trainer, administrator."""

from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from assessments.models import AnswerKey
from assessments.tests import SECRET_FILL, make_exam
from core.models import Activity, Module, ModuleNote, Unit
from core.tests import PDF_BYTES, BaseCase


class DashboardPage(BaseCase):
    """The student dashboard: what anonymous visitors and non-staff accounts see."""

    def get(self):
        return self.client.get(reverse("core:dashboard"))

    def test_uses_the_night_theme_and_its_assets(self):
        html = self.get().content.decode()
        self.assertIn('<body class="night">', html)
        self.assertIn("css/dashboard.css", html)
        self.assertIn("css/platform.css", html)
        self.assertIn("plus-jakarta-sans-latin-wght-normal.woff2", html)
        self.assertIn('data-bs-theme="dark"', html)           # one dark theme, site-wide
        self.assertNotIn("fonts.googleapis", html)            # fonts stay self-hosted
        self.assertNotIn("cdn.jsdelivr", html)

    def test_other_pages_are_not_themed(self):
        for name in ("core:browse", "core:search"):
            html = self.client.get(reverse(name)).content.decode()
            self.assertNotIn("night", html.split("<body", 1)[1].split(">", 1)[0], name)
            self.assertNotIn("dashboard.css", html, name)

    def test_empty_library_shows_a_single_guidance_card(self):
        for who in (None, self.student):
            self.client.logout()
            if who:
                self.client.force_login(who)
            html = self.get().content.decode()
            self.assertIn("Nothing has been published yet", html)
            self.assertIn("Trainers are still adding notes", html)
            self.assertEqual(html.count('class="nd-empty"'), 1)
            self.assertNotIn("Choose your level", html)
            self.assertNotIn('id="latest-title"', html)

    def test_published_note_appears_under_its_level_and_in_latest(self):
        note = self.make_note()
        self.client.logout()
        html = self.get().content.decode()
        self.assertIn("Choose your level", html)
        self.assertIn(self.level.name, html)
        self.assertIn("Latest from your trainers", html)
        self.assertIn(note.title, html)
        self.assertNotIn("Nothing has been published yet", html)

    def test_latest_is_one_list_newest_first_capped_at_five(self):
        first = self.make_note()
        for i in range(6):
            self.post_note(self.alice, title=f"Extra note {i}", module_data={"module": first.module_id})
        self.client.logout()
        html = self.get().content.decode()
        self.assertEqual(html.count('id="latest-title"'), 1)
        self.assertEqual(html.count("nd-row-title"), 5)
        self.assertIn("Extra note 5", html)                  # newest is in
        self.assertNotIn(first.title, html.split("latest-title", 1)[1])   # oldest dropped

    def test_drafts_never_appear_for_students(self):
        self.make_note(published=False)
        self.client.logout()
        r = self.get()
        self.assertNotContains(r, "Loops notes")
        self.assertContains(r, "Nothing has been published yet")

    def test_old_clutter_is_gone(self):
        self.make_note()
        self.client.force_login(self.student)
        html = self.get().content.decode()
        for gone in ("nd-hero", "nd-spot", "Recently viewed", "Just published", "nd-workspace"):
            self.assertNotIn(gone, html)

    def test_each_role_gets_its_own_dashboard(self):
        self.client.logout()
        self.assertContains(self.get(), "Student library")
        self.client.force_login(self.student)
        self.assertContains(self.get(), "Student library")
        self.client.force_login(self.alice)
        r = self.get()
        self.assertContains(r, "Trainer workspace")
        self.assertNotContains(r, "Student library")
        self.client.force_login(self.admin)
        r = self.get()
        self.assertContains(r, "Platform overview")
        self.assertNotContains(r, "Trainer workspace")


class DashboardActivities(BaseCase):
    """Published activities share the student "Latest" list; drafts never appear."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.module = Module.objects.create(trade=cls.level, key="GENCP302",
                                           code="GENCP302", name="C Programming")
        cls.topic = Unit.objects.create(module=cls.module, code="LO2", title="Loops")

    def make_activity(self, title, published=True, name="worksheet.pdf"):
        return Activity.objects.create(
            module=self.module, topic=self.topic, title=title, is_published=published,
            uploaded_by=self.alice,
            document=SimpleUploadedFile(name, PDF_BYTES, content_type="application/pdf"))

    def get(self):
        return self.client.get(reverse("core:dashboard"))

    def test_published_activity_is_listed_for_anonymous_visitors(self):
        activity = self.make_activity("Loops worksheet")
        self.client.logout()
        r = self.get()
        self.assertContains(r, 'id="latest-title"')
        self.assertContains(r, "Loops worksheet")
        self.assertContains(r, reverse("core:activity_detail", args=[activity.pk]))

    def test_published_activity_is_listed_for_signed_in_students(self):
        self.make_activity("Loops worksheet")
        self.client.force_login(self.student)
        self.assertContains(self.get(), "Loops worksheet")

    def test_draft_activities_never_appear(self):
        self.make_activity("Secret draft activity", published=False)
        self.client.logout()
        r = self.get()
        self.assertNotContains(r, "Secret draft activity")
        self.assertNotContains(r, 'id="latest-title"')

    def test_activities_open_in_the_reader_not_as_a_download(self):
        activity = self.make_activity("Loops worksheet")
        self.client.logout()
        html = self.get().content.decode()
        self.assertIn(reverse("core:activity_detail", args=[activity.pk]), html)
        self.assertNotIn(reverse("core:activity_file", args=[activity.pk]), html)


class TrainerDashboard(BaseCase):
    """A trainer sees only their own work: four numbers, recent uploads, their modules."""

    def get(self, who=None):
        self.client.force_login(who or self.alice)
        return self.client.get(reverse("core:dashboard"))

    def test_shows_four_numbers_and_the_quick_actions(self):
        html = self.get().content.decode()
        self.assertEqual(html.count('class="nd-stat"'), 4)
        for label in ("Notes", "Activities", "Open assessments", "Awaiting marking"):
            self.assertIn(label, html)
        for name in ("core:note_create", "core:activity_create", "assessments:exam_create"):
            self.assertIn(reverse(name), html)
        self.assertIn("Welcome back, Alice", html)

    def test_counts_only_this_trainers_notes_with_drafts_called_out(self):
        self.make_note(self.alice)                                 # published
        self.make_note(self.alice, published=False)                # draft
        self.ensure_module("GENCP302", "C Programming", self.bob)
        self.make_note(self.bob)                                   # someone else's
        r = self.get()
        notes = next(s for s in r.context["stats"] if s["label"] == "Notes")
        self.assertEqual(notes["value"], 1)
        self.assertEqual(notes["sub"], "1 draft")
        self.assertTrue(notes["warn"])

    def test_recent_uploads_are_mine_with_a_status_badge_and_link_to_edit(self):
        mine = self.make_note(self.alice, published=False)
        self.ensure_module("GENCP302", "C Programming", self.bob)
        theirs = self.make_note(self.bob)
        html = self.get().content.decode()
        self.assertIn(mine.title, html)
        self.assertIn("nd-badge-draft", html)
        self.assertIn(reverse("core:note_edit", args=[mine.pk]), html)
        self.assertNotIn(reverse("core:note_edit", args=[theirs.pk]), html)

    def test_lists_assigned_modules_or_explains_there_are_none(self):
        self.assertContains(self.get(), "No modules are assigned to you yet")
        self.ensure_module("GENCP302", "C Programming", self.alice)
        r = self.get()
        self.assertContains(r, "GENCP302")
        self.assertNotContains(r, "No modules are assigned")

    def test_empty_state_invites_the_first_upload(self):
        r = self.get()
        self.assertContains(r, "Add your first notes")
        self.assertNotContains(r, "Choose your level")            # no student content here


class AdminDashboard(BaseCase):
    """Administrators see platform totals and only the things that need action."""

    def get(self):
        self.client.force_login(self.admin)
        return self.client.get(reverse("core:dashboard"))

    def test_shows_four_totals_and_two_actions(self):
        html = self.get().content.decode()
        self.assertEqual(html.count('class="nd-stat"'), 4)
        for label in ("Trainers", "Modules", "Published content", "Open assessments"):
            self.assertIn(label, html)
        self.assertIn(reverse("accounts:create_trainer"), html)
        self.assertIn(reverse("core:curriculum"), html)

    def test_all_clear_when_nothing_needs_attention(self):
        self.ensure_module("GENCP302", "C Programming", self.alice, self.bob)
        r = self.get()
        self.assertEqual(r.context["attention"], [])
        self.assertContains(r, "Nothing needs attention right now")

    def test_flags_trainers_without_modules_and_unpublished_drafts(self):
        self.ensure_module("GENCP302", "C Programming", self.alice)       # bob has none
        self.make_note(self.alice, published=False)
        r = self.get()
        texts = [a["text"] for a in r.context["attention"]]
        self.assertIn("1 trainer without modules", texts)
        self.assertIn("1 draft not yet published", texts)
        self.assertContains(r, reverse("accounts:manage_trainers"))

    def test_latest_uploads_span_all_trainers_and_name_the_author(self):
        self.ensure_module("GENCP302", "C Programming", self.alice, self.bob)
        a, b = self.make_note(self.alice), self.make_note(self.bob)
        html = self.get().content.decode()
        self.assertIn(reverse("core:note_edit", args=[a.pk]), html)
        self.assertIn(reverse("core:note_edit", args=[b.pk]), html)
        self.assertIn("Alice", html.split("Latest uploads", 1)[1])
        self.assertIn("Bob", html.split("Latest uploads", 1)[1])

    def test_never_links_to_the_retired_django_admin(self):
        self.assertNotContains(self.get(), 'href="/admin/')


class StudentSeesEveryOpenAssessment(BaseCase):
    """No cap: every open assessment that is meant for the dashboard is listed."""

    def test_all_open_assessments_are_listed_not_just_three(self):
        for _ in range(5):
            make_exam(self.alice)
        self.client.logout()
        html = self.client.get(reverse("core:dashboard")).content.decode()
        self.assertEqual(html.count('class="nd-assess-card'), 5)
        self.assertIn('<span class="nd-count">5</span>', html)

    def test_closed_or_hidden_assessments_are_still_left_out(self):
        make_exam(self.alice)
        make_exam(self.alice, is_open=False)
        make_exam(self.alice, show_on_dashboard=False)
        self.client.logout()
        html = self.client.get(reverse("core:dashboard")).content.decode()
        self.assertEqual(html.count('class="nd-assess-card'), 1)


class StaffAssessmentButtons(BaseCase):
    """Trainer and administrator dashboards: one Marking guide button per assessment."""

    def test_trainer_gets_a_button_for_each_of_their_assessments_only(self):
        mine = [make_exam(self.alice)[0], make_exam(self.alice, is_open=False)[0]]
        theirs = make_exam(self.bob)[0]
        self.client.force_login(self.alice)
        html = self.client.get(reverse("core:dashboard")).content.decode()
        self.assertEqual(html.count("Marking guide</a>"), 2)
        for e in mine:
            self.assertIn(reverse("assessments:exam_guide", args=[e.pk]), html)
            self.assertIn(reverse("assessments:exam_detail", args=[e.pk]), html)
        self.assertNotIn(reverse("assessments:exam_guide", args=[theirs.pk]), html)

    def test_admin_gets_a_button_for_every_assessment_with_the_owner(self):
        a, b = make_exam(self.alice)[0], make_exam(self.bob)[0]
        self.client.force_login(self.admin)
        html = self.client.get(reverse("core:dashboard")).content.decode()
        self.assertEqual(html.count("Marking guide</a>"), 2)
        self.assertIn(reverse("assessments:exam_guide", args=[a.pk]), html)
        self.assertIn(reverse("assessments:exam_guide", args=[b.pk]), html)
        self.assertIn("by Alice", html)
        self.assertIn("by Bob", html)

    def test_shows_how_many_written_questions_lack_a_guide(self):
        exam, qs = make_exam(self.alice)
        AnswerKey.objects.filter(question=qs[("open", 1)]).update(data={"guide": ""})
        self.client.force_login(self.alice)
        self.assertContains(self.client.get(reverse("core:dashboard")), "1 without guide")

    def test_empty_state_offers_a_new_assessment(self):
        self.client.force_login(self.alice)
        r = self.client.get(reverse("core:dashboard"))
        self.assertContains(r, "No assessments have been created yet")
        self.assertContains(r, reverse("assessments:exam_create"))


class MarkingGuidePage(BaseCase):
    """The page the dashboard button opens."""

    def setUp(self):
        super().setUp()
        self.exam, self.qs = make_exam(self.alice)
        self.url = reverse("assessments:exam_guide", args=[self.exam.pk])

    def test_owner_sees_every_answer_and_the_written_guide(self):
        self.client.force_login(self.alice)
        r = self.client.get(self.url)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Mentions leases and automatic addressing.")   # written guide
        self.assertContains(r, SECRET_FILL)                                   # fill-in answers
        self.assertContains(r, "(correct answer)")                            # multiple choice
        self.assertContains(r, "Connects networks")                           # matching pairs
        self.assertContains(r, reverse("assessments:question_edit",
                                       args=[self.exam.pk, self.qs[("open", 1)].pk]))

    def test_a_missing_guide_is_flagged_with_an_add_button(self):
        AnswerKey.objects.filter(question=self.qs[("open", 1)]).update(data={"guide": ""})
        self.client.force_login(self.alice)
        r = self.client.get(self.url)
        self.assertContains(r, "No marking guide yet")
        self.assertContains(r, "Add guide")

    def test_admin_can_open_any_guide(self):
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_another_trainer_students_and_visitors_are_kept_out(self):
        self.client.force_login(self.bob)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.client.force_login(self.student)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.client.logout()
        r = self.client.get(self.url)
        self.assertEqual(r.status_code, 302)
        self.assertIn("login", r["Location"])

    def test_assessment_page_links_to_its_guide(self):
        self.client.force_login(self.alice)
        r = self.client.get(reverse("assessments:exam_detail", args=[self.exam.pk]))
        self.assertContains(r, self.url)


class SiteHeaderLayout(BaseCase):
    """Header = logo + user menu; then one menu bar; then the search field."""

    def html(self, who=None):
        self.client.logout()
        if who:
            self.client.force_login(who)
        return self.client.get(reverse("core:dashboard")).content.decode()

    def test_three_rows_in_order(self):
        html = self.html()
        top, menu, search = (html.index(x) for x in
                             ("app-topbar", "app-menubar", "app-searchbar"))
        self.assertLess(top, menu)
        self.assertLess(menu, search)

    def test_header_row_holds_only_brand_and_user_area(self):
        top = self.html(self.alice).split('class="app-topbar"', 1)[1].split("</header>", 1)[0]
        self.assertIn("brand-mark", top)
        self.assertIn("user-toggle", top)
        self.assertNotIn("nav-link", top)
        self.assertNotIn('type="search"', top)

    def test_menu_bar_is_role_aware(self):
        def menu(who):
            h = self.html(who)
            return h.split('class="app-menubar"', 1)[1].split("</nav>", 1)[0]
        anon, trainer, admin = menu(None), menu(self.alice), menu(self.admin)
        for text in ("Dashboard", "Browse"):
            self.assertIn(text, anon)
        self.assertNotIn("notes", anon.lower().replace("module notes", ""))
        self.assertNotIn("Curriculum", trainer)
        self.assertIn("My notes", trainer)
        self.assertIn("My activities", trainer)
        for text in ("All notes", "All activities", "Curriculum", "Trainers"):
            self.assertIn(text, admin)

    def test_search_lives_under_the_menu_and_is_not_duplicated_in_the_hero(self):
        html = self.html()
        self.assertEqual(html.count('type="search"'), 1)
        self.assertIn(reverse("core:search"), html.split("app-searchbar", 1)[1])

    def test_dashboard_uses_the_shared_palette_not_a_private_dark_one(self):
        css = open("static/css/dashboard.css", encoding="utf-8").read()
        self.assertNotIn("color-scheme: dark", css)
        self.assertNotIn("#090e1a", css)
        self.assertIn("var(--c-brand)", css)

    def test_search_page_does_not_show_a_second_search_field(self):
        r = self.client.get(reverse("core:search"))
        self.assertEqual(r.content.decode().count('type="search"'), 1)


class DarkTheme(BaseCase):
    """One dark palette for every dashboard: public site, workspaces and Django admin."""

    def css(self, path):
        return open(path, encoding="utf-8").read()

    def test_every_page_asks_bootstrap_for_dark_mode(self):
        for name in ("core:dashboard", "core:browse", "core:search"):
            html = self.client.get(reverse(name)).content.decode()
            self.assertIn('data-bs-theme="dark"', html, name)
        self.client.force_login(self.alice)
        for name in ("core:notes_manage", "core:activities_manage"):
            html = self.client.get(reverse(name)).content.decode()
            self.assertIn('data-bs-theme="dark"', html, name)

    def test_shared_tokens_are_dark(self):
        css = self.css("static/css/platform.css")
        self.assertIn("color-scheme: dark;", css)
        self.assertIn("--c-bg:         #0a101d;", css)
        self.assertNotIn("rgba(255, 255, 255, .94)", css)    # no white header bar left

    def test_white_text_only_sits_on_solid_cobalt(self):
        css = self.css("static/css/platform.css")
        self.assertIn("--bs-btn-bg: var(--c-brand-solid)", css)
        self.assertIn(".btn-danger { --bs-btn-bg: var(--c-danger-solid)", css)

    def test_note_reader_keeps_a_light_sheet_of_paper(self):
        css = self.css("static/css/platform.css")
        paper = css.split("#note-prose {", 1)[1].split("}", 1)[0]
        self.assertIn("color-scheme: light;", paper)
        self.assertIn("--c-text: #0f172a;", paper)

    def test_admin_uses_the_same_dark_palette_in_every_theme_mode(self):
        css = self.css("static/css/admin-theme.css")
        self.assertIn("--page-bg: #0a101d;", css)
        self.assertNotIn("--page-bg: #eef1f4", css)
        self.assertNotIn("prefers-color-scheme: light", css)
        self.assertIn(".theme-toggle { display: none !important; }", css)

    def test_dashboard_accents_use_the_shared_cobalt(self):
        css = self.css("static/css/dashboard.css")
        self.assertIn("var(--c-brand-solid)", css)


class MenuBarWidth(BaseCase):
    def test_menu_bar_is_a_compact_pill_not_a_full_width_strip(self):
        css = open("static/css/platform.css", encoding="utf-8").read()
        menu = css.split(".app-menu {", 1)[1].split("}", 1)[0]
        self.assertIn("width: fit-content", menu)
        self.assertIn("max-width: 100%", menu)
        self.assertIn("border-radius: 999px", menu)
        bar = css.split(".app-menubar {", 1)[1].split("}", 1)[0]
        self.assertNotIn("background", bar)       # no full-width band behind the pill
