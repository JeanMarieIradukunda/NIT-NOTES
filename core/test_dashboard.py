"""Tests for the dark student dashboard (landing page)."""

from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from core.models import Activity, Module, ModuleNote, Unit
from core.tests import PDF_BYTES, BaseCase


class DashboardPage(BaseCase):
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

    def test_empty_library_shows_guidance_for_students_and_trainers(self):
        self.client.logout()
        self.assertContains(self.get(), "Trainers are still adding notes")
        self.assertContains(self.get(), "Levels will appear here")
        self.client.force_login(self.alice)
        r = self.get()
        self.assertContains(r, "Add your first module notes")
        self.assertContains(r, reverse("core:note_create"))

    def test_newest_published_note_is_the_spotlight_and_rest_are_listed(self):
        first = self.make_note()
        self.post_note(self.alice, title="Second published note",
                       module_data={"module": first.module_id})
        second = ModuleNote.objects.latest("pk")
        self.assertNotEqual(first.pk, second.pk)
        self.client.logout()
        html = self.get().content.decode()
        self.assertIn("Latest from your trainers", html)
        # Spotlight holds the newest note, the list below holds the others.
        spot = html.split('class="nd-spot"', 1)[1].split("</a>", 1)[0]
        self.assertIn(second.title, spot)
        self.assertNotIn(second.title, html.split("Just published", 1)[1])
        self.assertIn(first.title, html.split("Just published", 1)[1])

    def test_single_note_has_no_duplicate_feed_and_levels_span_the_page(self):
        self.make_note()
        self.client.logout()
        html = self.get().content.decode()
        self.assertNotIn('id="whats-new-title"', html)
        self.assertIn("nd-body--solo", html)
        self.assertIn("Choose your level", html)

    def test_drafts_never_appear_for_students(self):
        self.make_note(published=False)
        self.client.logout()
        r = self.get()
        self.assertNotContains(r, "Loops notes")
        self.assertContains(r, "Trainers are still adding notes")

    def test_trainer_workspace_is_hidden_from_students(self):
        self.make_note()
        self.client.logout()
        self.assertNotContains(self.get(), "Your trainer workspace")
        self.client.force_login(self.alice)
        r = self.get()
        self.assertContains(r, "Your trainer workspace")
        self.assertContains(r, "Add module notes")
        self.client.force_login(self.admin)
        self.assertContains(self.get(), "Notes workspace")


class DashboardActivities(BaseCase):
    """Published activities appear on the dashboard for everyone; drafts never do."""

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
        self.assertContains(r, 'id="activities-title"')
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
        self.assertNotContains(r, 'id="activities-title"')

    def test_activities_open_in_the_reader_not_as_a_download(self):
        activity = self.make_activity("Loops worksheet")
        self.client.logout()
        html = self.get().content.decode()
        self.assertIn(reverse("core:activity_detail", args=[activity.pk]), html)
        self.assertNotIn(reverse("core:activity_file", args=[activity.pk]), html)


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
