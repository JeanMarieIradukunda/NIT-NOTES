"""IT Knowledge Challenge: card generator, SVG cleaning, gallery, permissions and the trainer workspace."""

import itertools
import xml.etree.ElementTree as ET
from io import StringIO

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from core.models import Module, Trade

from . import brand, characters, render
from .models import KnowledgeCard
from .svg_clean import MAX_BYTES, SVGError, clean_svg

User = get_user_model()
PLAIN = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
SVG_NS = "{http://www.w3.org/2000/svg}"
PARTS = dict(title="Ping Pong", bubble="I send echo requests.", question="Which command checks reachability?",
             answer="ping", explanation="ping sends ICMP echo requests.", character="router", mood="smug",
             module_code="NETWK301", module_name="Networking", number=3, palette=1)


class RenderTests(SimpleTestCase):
    def test_every_character_and_mood_gives_valid_svg_of_the_right_size(self):
        for ch, mood in itertools.product(characters.CHARACTER_KEYS, characters.MOOD_KEYS):
            root = ET.fromstring(render.render_card({**PARTS, "character": ch, "mood": mood}))
            self.assertEqual(root.get("viewBox"), "0 0 1080 1080", (ch, mood))

    def test_a_card_has_all_its_parts_and_the_brand(self):
        svg = render.render_card(PARTS, "Padri Vjeko Centre TSS")
        for needle in ("Ping Pong", "I send echo requests.", "QUESTION", "Which command checks reachability?", "ANSWER",
                       "ping", "Why? ping sends ICMP echo requests.", brand.NAME, brand.TAGLINE, "NETWK301", "#03",
                       "Padri Vjeko Centre TSS"):
            self.assertIn(needle, svg)

    def test_text_is_escaped_so_a_card_can_never_carry_markup(self):
        svg = render.render_card({**PARTS, "title": "<script>alert(1)</script>", "bubble": 'x" onload="evil()'})
        ET.fromstring(svg)
        self.assertNotIn("<script", svg)
        self.assertNotIn('onload="evil()"', svg)
        self.assertEqual([e for e in ET.fromstring(svg).iter() if e.tag.endswith("script")], [])

    def test_over_long_text_is_cut_not_spilled(self):
        long = render.render_card({**{k: "word " * 120 for k in render.LIMITS}, "character": "laptop", "mood": "happy",
                                   "module_code": "X" * 40, "number": 7, "palette": 2})
        ET.fromstring(long)
        self.assertIn("…", long)

    def test_the_text_stays_inside_its_panels_even_in_a_wide_font(self):
        try:
            from PIL import ImageFont
            bold = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 20)
            regular = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 20)
        except Exception:
            self.skipTest("needs Pillow and DejaVu to measure text")
        words = "network router switch packet address server client protocol firewall latency bandwidth gateway".split()
        regions = {"title": ((100, 260), 940), "bubble": ((300, 520), 450), "question": ((640, 740), 890),
                   "answer": ((800, 884), 890), "explanation": ((900, 1000), 950)}
        for key, ((lo, hi), maxw) in regions.items():
            text = " ".join(itertools.islice(itertools.cycle(words), render.LIMITS[key] // 7))[:render.LIMITS[key]]
            root = ET.fromstring(render.render_card({**PARTS, key: text}))
            for t in root.iter(SVG_NS + "text"):
                if lo <= float(t.get("y")) <= hi and t.text:
                    size = float(t.get("font-size"))
                    font = (bold if int(t.get("font-weight", "400")) >= 600 else regular).font_variant(size=size)
                    self.assertLessEqual(font.getlength(t.text), maxw + 1, (key, t.text))

    def test_the_palette_follows_the_topic(self):
        self.assertEqual(render.palette_for(1), render.palette_for(1 + len(render.PALETTES)))
        self.assertNotEqual(render.palette_for(0), render.palette_for(1))


class CleanSvgTests(SimpleTestCase):
    def wrap(self, body, attrs='viewBox="0 0 1080 1080"'):
        return f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" {attrs}>{body}</svg>'

    def test_scripts_handlers_and_embedded_pages_are_removed(self):
        out, _ = clean_svg(self.wrap(
            '<script>alert(1)</script><foreignObject><div>x</div></foreignObject>'
            '<rect width="5" height="5" onclick="evil()" onload="evil()"/><iframe src="http://x"/>'
            '<a xlink:href="javascript:alert(1)"><circle r="3"/></a>'))
        for bad in ("script", "foreignObject", "onclick", "onload", "iframe", "javascript:"):
            self.assertNotIn(bad, out)
        self.assertIn("<rect", out)

    def test_links_to_other_sites_are_removed_but_internal_and_embedded_images_stay(self):
        png = "data:image/png;base64,iVBORw0KGgo="
        out, _ = clean_svg(self.wrap(
            f'<defs><path id="p" d="M0 0"/></defs><use xlink:href="#p"/><image href="http://evil.example/x.png"/>'
            f'<image href="{png}" width="1" height="1"/><image href="data:text/html;base64,AAAA"/>'))
        self.assertNotIn("evil.example", out)
        self.assertNotIn("text/html", out)
        self.assertIn("#p", out)
        self.assertIn(png, out)

    def test_dangerous_styles_are_removed(self):
        out, _ = clean_svg(self.wrap('<style>@import url(http://evil/x.css); .a{fill:red}</style>'
                                     '<rect style="fill:url(http://evil/p)" width="1" height="1"/><rect class="a" width="1" height="1"/>'))
        self.assertNotIn("@import", out)
        self.assertNotIn("evil", out)
        self.assertIn("fill:red", out)

    def test_entity_tricks_and_non_svg_are_refused(self):
        with self.assertRaises(SVGError):
            clean_svg('<?xml version="1.0"?><!DOCTYPE svg [<!ENTITY a "aaaa">]><svg xmlns="http://www.w3.org/2000/svg">&a;</svg>')
        with self.assertRaises(SVGError):
            clean_svg("<html><body>hi</body></html>")
        with self.assertRaises(SVGError):
            clean_svg("this is not xml at all <<<")
        with self.assertRaises(SVGError):
            clean_svg(b"\xff\xfe\x00bad")
        with self.assertRaises(SVGError):
            clean_svg(b"<svg xmlns='http://www.w3.org/2000/svg'>" + b" " * MAX_BYTES + b"</svg>")

    def test_size_and_shape_notes(self):
        out, warnings = clean_svg(self.wrap('<rect width="1" height="1"/>', 'width="500" height="500"'))
        self.assertIn('viewBox="0 0 1080 1080"', out)
        self.assertNotIn('width="500"', out)
        self.assertTrue(any("viewBox" in w for w in warnings))
        _, warnings = clean_svg(self.wrap('<rect width="1" height="1"/>', 'viewBox="0 0 800 400"'))
        self.assertTrue(any("800 x 400" in w for w in warnings))
        _, warnings = clean_svg(self.wrap('<rect width="1" height="1"/>'))
        self.assertEqual(warnings, [])


@override_settings(STORAGES=PLAIN)
class ChallengeBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.trainer_group = Group.objects.get_or_create(name="Trainer")[0]
        cls.level = Trade.objects.create(key="l3", name="Level 3", order=1)
        cls.net = Module.objects.create(trade=cls.level, key="NETWK301", code="NETWK301", name="Networking", order=1)
        cls.sec = Module.objects.create(trade=cls.level, key="SECUR302", code="SECUR302", name="Security", order=2)
        cls.alice = cls.user("alice", True)
        cls.bob = cls.user("bob", True)
        cls.alice.profile.trainer_modules.add(cls.net)
        cls.bob.profile.trainer_modules.add(cls.sec)
        cls.admin = User.objects.create_superuser("root", "root@example.com", "pw-12345!")
        cls.student = cls.user("student", False)

    @staticmethod
    def user(name, trainer):
        u = User.objects.create_user(name, f"{name}@example.com", "pw-12345!")
        if trainer:
            u.groups.add(Group.objects.get(name="Trainer"))
        return u

    def card(self, module=None, owner=None, **extra):
        data = dict(title="Ping Pong", bubble="I send echo requests.", question="Which command checks reachability?",
                    answer="ping", explanation="ping sends ICMP echo requests.", character="router", mood="smug")
        data.update(extra)
        return KnowledgeCard.objects.create(module=module or self.net, uploaded_by=owner or self.alice, **data)

    def form_data(self, **extra):
        data = dict(module=self.net.pk, title="New card", character="laptop", mood="happy", bubble="Hello there",
                    question="A question?", answer="An answer", explanation="Because.", is_published="on")
        data.update(extra)
        return data


class ModelTests(ChallengeBase):
    def test_numbers_count_up_within_a_module(self):
        a, b, c = self.card(title="A"), self.card(title="B"), self.card(self.sec, self.bob, title="C")
        self.assertEqual((a.number, b.number, c.number), (1, 2, 1))

    def test_the_picture_is_generated_or_your_own(self):
        c = self.card()
        root = ET.fromstring(c.svg())
        self.assertEqual(root.get("viewBox"), "0 0 1080 1080")
        self.assertIn("NETWK301", c.svg())
        c.custom_svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1080 1080"><rect width="9" height="9"/></svg>'
        self.assertEqual(c.svg(), c.custom_svg)
        self.assertTrue(c.has_custom_design)

    def test_alt_text_and_addresses(self):
        c = self.card()
        self.assertIn("Question: Which command checks reachability?", c.alt_text)
        self.assertIn(f"/challenge/c/{c.pk}/picture/?v=", c.svg_url)
        self.assertEqual(c.svg_url, c.picture_url)
        self.assertEqual(c.get_absolute_url(), f"/challenge/c/{c.pk}/")
        self.assertTrue(c.download_name.startswith("IT-Knowledge-Challenge-NETWK301-01-ping-pong"))


class GalleryTests(ChallengeBase):
    def setUp(self):
        self.shown = self.card(title="Shown one")
        self.sec_card = self.card(self.sec, self.bob, title="Security one")
        self.hidden = self.card(title="Secret draft", is_published=False)

    def test_anyone_can_see_published_cards_only(self):
        page = self.client.get(reverse("challenge:gallery"))
        self.assertContains(page, "Shown one")
        self.assertContains(page, "Security one")
        self.assertNotContains(page, "Secret draft")

    def test_branding_chips_grid_and_viewer(self):
        page = self.client.get(reverse("challenge:gallery"))
        for needle in ("IT KNOWLEDGE CHALLENGE", "Learn IT. Laugh IT. Master IT!", 'id="kcChips"', 'class="kc-grid"',
                       'id="kcViewer"', 'role="dialog"', "Previous", "Next", "NETWK301", "SECUR302", "challenge.js"):
            self.assertContains(page, needle)
        self.assertContains(page, "kc-chip active")

    def test_topic_chips_count_and_filter_from_the_address(self):
        page = self.client.get(reverse("challenge:gallery") + f"?topic={self.sec.pk}")
        html = page.content.decode()
        self.assertIn(f'href="/challenge/?topic={self.sec.pk}"', html)
        import re
        hidden = re.findall(r'<li class="kc-item" hidden data-topic="(\d+)"', html)
        self.assertEqual(hidden, [str(self.net.pk)])
        unknown = self.client.get(reverse("challenge:gallery") + "?topic=99999").content.decode()
        self.assertNotIn(' hidden data-topic', unknown)

    def test_empty_gallery_still_renders(self):
        KnowledgeCard.objects.all().delete()
        self.assertContains(self.client.get(reverse("challenge:gallery")), "No cards yet")

    def test_the_menu_links_to_the_challenge(self):
        self.assertContains(self.client.get(reverse("core:dashboard")), reverse("challenge:gallery"))

    def test_trainers_see_the_add_button_students_do_not(self):
        self.assertNotContains(self.client.get(reverse("challenge:gallery")), "Add a card")
        self.client.force_login(self.alice)
        self.assertContains(self.client.get(reverse("challenge:gallery")), "Add a card")

    def test_the_viewer_script_has_what_was_asked_for(self):
        from pathlib import Path
        from django.conf import settings
        js = (Path(settings.BASE_DIR) / "static/js/challenge.js").read_text(encoding="utf-8")
        for needle in ("kcPrev", "kcNext", "ArrowLeft", "ArrowRight", "Escape", "touchstart", "applyFilter", "aria-current"):
            self.assertIn(needle, js)
        css = (Path(settings.BASE_DIR) / "static/css/challenge.css").read_text(encoding="utf-8")
        for needle in ("@media (max-width: 767px)", "prefers-reduced-motion", "var(--c-surface)", "overflow-x: auto"):
            self.assertIn(needle, css)


class CardPageAndPictureTests(ChallengeBase):
    def setUp(self):
        self.pub = self.card(title="Public card")
        self.draft = self.card(title="Draft card", is_published=False)

    def test_a_published_card_has_a_public_page_with_a_text_version(self):
        page = self.client.get(self.pub.get_absolute_url())
        self.assertContains(page, "Public card")
        self.assertContains(page, "Read it as text")
        self.assertContains(page, "Which command checks reachability?")
        self.assertContains(page, "download=1")

    def test_a_draft_is_hidden_from_everyone_but_its_author_and_administrators(self):
        for who in (None, self.student, self.bob):
            if who:
                self.client.force_login(who)
            self.assertEqual(self.client.get(self.draft.get_absolute_url()).status_code, 404)
            self.assertEqual(self.client.get(reverse("challenge:card_svg", args=[self.draft.pk])).status_code, 404)
            self.client.logout()
        for who in (self.alice, self.admin):
            self.client.force_login(who)
            self.assertEqual(self.client.get(self.draft.get_absolute_url()).status_code, 200)
            self.client.logout()

    def test_the_picture_is_served_as_an_inert_image(self):
        r = self.client.get(reverse("challenge:card_svg", args=[self.pub.pk]))
        self.assertEqual(r["Content-Type"], "image/svg+xml; charset=utf-8")
        self.assertIn("default-src 'none'", r["Content-Security-Policy"])
        self.assertIn("sandbox", r["Content-Security-Policy"])
        self.assertEqual(r["X-Content-Type-Options"], "nosniff")
        self.assertIn("max-age=86400", r["Cache-Control"])
        ET.fromstring(r.content)

    def test_drafts_are_never_cached_by_shared_caches(self):
        self.client.force_login(self.alice)
        self.assertEqual(self.client.get(reverse("challenge:card_svg", args=[self.draft.pk]))["Cache-Control"], "private, no-store")

    def test_download_gets_a_sensible_file_name(self):
        r = self.client.get(reverse("challenge:card_svg", args=[self.pub.pk]) + "?download=1")
        self.assertIn("attachment", r["Content-Disposition"])
        self.assertIn("IT-Knowledge-Challenge-NETWK301", r["Content-Disposition"])

    def test_previous_and_next_follow_the_published_order(self):
        second = self.card(title="Second")
        page = self.client.get(self.pub.get_absolute_url())
        self.assertContains(page, second.get_absolute_url())
        self.assertNotContains(page, self.draft.get_absolute_url())


class WorkspaceTests(ChallengeBase):
    def post_new(self, user, **extra):
        self.client.force_login(user)
        return self.client.post(reverse("challenge:card_create"), self.form_data(**extra))

    def test_only_trainers_reach_the_workspace(self):
        for name in ("manage", "card_create"):
            self.assertEqual(self.client.get(reverse(f"challenge:{name}")).status_code, 302)       # to sign in
        self.client.force_login(self.student)
        for name in ("manage", "card_create"):
            self.assertEqual(self.client.get(reverse(f"challenge:{name}")).status_code, 403)

    def test_a_trainer_adds_a_card_under_their_own_module(self):
        r = self.post_new(self.alice)
        self.assertRedirects(r, reverse("challenge:manage"))
        card = KnowledgeCard.objects.get(title="New card")
        self.assertEqual((card.uploaded_by, card.module, card.number), (self.alice, self.net, 1))

    def test_a_trainer_cannot_file_a_card_under_someone_elses_module(self):
        r = self.post_new(self.alice, module=self.sec.pk)
        self.assertEqual(r.status_code, 200)
        self.assertFalse(KnowledgeCard.objects.exists())

    def test_an_administrator_can_use_any_module(self):
        self.post_new(self.admin, module=self.sec.pk)
        self.assertEqual(KnowledgeCard.objects.get().module, self.sec)

    def test_text_limits_are_enforced(self):
        r = self.post_new(self.alice, title="x" * (render.LIMITS["title"] + 1))
        self.assertEqual(r.status_code, 200)
        self.assertFalse(KnowledgeCard.objects.exists())

    def test_my_cards_lists_only_mine_but_an_administrator_sees_all(self):
        mine, theirs = self.card(title="Mine"), self.card(self.sec, self.bob, title="Theirs")
        self.client.force_login(self.alice)
        page = self.client.get(reverse("challenge:manage"))
        self.assertContains(page, "Mine")
        self.assertNotContains(page, "Theirs")
        self.client.force_login(self.admin)
        page = self.client.get(reverse("challenge:manage"))
        self.assertContains(page, "Mine")
        self.assertContains(page, "Theirs")

    def test_editing_is_for_the_author_or_an_administrator(self):
        card = self.card()
        url = reverse("challenge:card_edit", args=[card.pk])
        self.client.force_login(self.bob)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.client.post(url, self.form_data(title="Hacked")).status_code, 403)
        self.client.force_login(self.alice)
        self.assertRedirects(self.client.post(url, self.form_data(title="Renamed")), reverse("challenge:manage"))
        self.client.force_login(self.admin)
        self.assertEqual(self.client.post(url, self.form_data(title="By admin", module=self.net.pk)).status_code, 302)
        card.refresh_from_db()
        self.assertEqual((card.title, card.uploaded_by), ("By admin", self.alice))

    def test_show_hide_and_delete_follow_the_same_rules(self):
        card = self.card()
        toggle, delete = reverse("challenge:card_toggle", args=[card.pk]), reverse("challenge:card_delete", args=[card.pk])
        self.client.force_login(self.bob)
        self.client.post(toggle)
        self.client.post(delete)
        card.refresh_from_db()
        self.assertTrue(card.is_published)
        self.client.force_login(self.alice)
        self.client.post(toggle)
        card.refresh_from_db()
        self.assertFalse(card.is_published)
        self.assertContains(self.client.get(delete), "cannot be undone")
        self.client.post(delete)
        self.assertFalse(KnowledgeCard.objects.exists())

    def test_toggle_needs_post(self):
        card = self.card()
        self.client.force_login(self.alice)
        self.assertEqual(self.client.get(reverse("challenge:card_toggle", args=[card.pk])).status_code, 405)

    def test_an_uploaded_design_is_cleaned_and_can_be_removed(self):
        dirty = (b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1080 1080"><script>alert(1)</script>'
                 b'<rect width="9" height="9" onclick="x()"/></svg>')
        self.post_new(self.alice, svg_file=SimpleUploadedFile("mine.svg", dirty, "image/svg+xml"))
        card = KnowledgeCard.objects.get()
        self.assertTrue(card.has_custom_design)
        self.assertNotIn("script", card.custom_svg)
        self.assertNotIn("onclick", card.custom_svg)
        self.assertEqual(card.svg(), card.custom_svg)
        self.client.post(reverse("challenge:card_edit", args=[card.pk]), self.form_data(remove_svg="on"))
        card.refresh_from_db()
        self.assertFalse(card.has_custom_design)

    def test_an_unusable_upload_is_refused_with_a_reason(self):
        r = self.post_new(self.alice, svg_file=SimpleUploadedFile("x.svg", b"not an svg", "image/svg+xml"))
        self.assertContains(r, "could not be read")
        self.assertFalse(KnowledgeCard.objects.exists())

    def test_a_hidden_card_says_so(self):
        r = self.client.post(reverse("challenge:card_create"), self.form_data(is_published=""), follow=False) \
            if self.client.login(username="alice", password="pw-12345!") else None
        self.assertRedirects(r, reverse("challenge:manage"))
        self.assertFalse(KnowledgeCard.objects.get().is_published)

    def test_the_live_preview_draws_the_card_without_saving(self):
        self.client.force_login(self.alice)
        r = self.client.post(reverse("challenge:card_preview"), self.form_data(title="Preview me", number="5"))
        self.assertEqual(r.status_code, 200)
        self.assertIn("Preview me", r.content.decode())
        self.assertIn("default-src 'none'", r["Content-Security-Policy"])
        self.assertFalse(KnowledgeCard.objects.exists())
        self.client.logout()
        self.assertEqual(self.client.post(reverse("challenge:card_preview"), self.form_data()).status_code, 302)

    def test_the_form_page_has_the_preview_and_counters(self):
        self.client.force_login(self.alice)
        page = self.client.get(reverse("challenge:card_create"))
        for needle in ('id="kcPreview"', "data-preview", "challenge-form.js", 'data-count="60"'):
            self.assertContains(page, needle)


class SeedCommandTests(ChallengeBase):
    def run_seed(self, *args):
        out = StringIO()
        call_command("seed_knowledge_cards", *args, stdout=out)
        return out.getvalue()

    def test_adds_starter_cards_once_and_they_all_render(self):
        self.assertIn("Added 9 cards to NETWK301", self.run_seed("--module", "NETWK301"))
        self.assertIn("Added 0 cards", self.run_seed("--module", "network301".replace("network", "netwk")))
        self.assertEqual(KnowledgeCard.objects.filter(module=self.net).count(), 9)
        for c in KnowledgeCard.objects.all():
            ET.fromstring(c.svg())
            for key, limit in render.LIMITS.items():
                self.assertLessEqual(len(getattr(c, key)), limit, (c.title, key))

    def test_draft_author_default_module_and_errors(self):
        self.run_seed("--module", "SECUR302", "--draft", "--user", "bob")
        self.assertEqual(KnowledgeCard.objects.filter(is_published=True).count(), 0)
        self.assertEqual(KnowledgeCard.objects.first().uploaded_by, self.bob)
        with self.assertRaises(CommandError):
            self.run_seed("--module", "NOPE")
        with self.assertRaises(CommandError):
            self.run_seed("--user", "ghost")
        self.assertIn("NETWK301", self.run_seed())                       # default: the first module
