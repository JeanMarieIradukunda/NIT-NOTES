"""
Tests for module notes (Trainer upload / publish / manage), the module-data
purge command, the hardened HTML sanitiser, and a render smoke test of every
page against an empty database.

Run with:  python manage.py test
"""

import shutil
import tempfile
from io import StringIO
from pathlib import Path
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.files.storage import FileSystemStorage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse

from core.html_processing import process_lesson_html
from core.models import (Lesson, Module, ModuleNote, Resource, StudentActivity,
                         Trade, Unit)

User = get_user_model()

PDF_BYTES = b"%PDF-1.4\n1 0 obj<< /Type /Catalog >>endobj\ntrailer<< /Root 1 0 R >>\n%%EOF\n"
HTML_OK = (
    "<html><head><title>Loops</title><script>alert('x')</script></head><body>"
    "<h1>Loops in C</h1>"
    "<h2>The for loop</h2><p>A for loop repeats a block of statements a fixed number of "
    "times, which makes it ideal for counting and for walking through arrays element by element.</p>"
    "<h2>The while loop</h2><p>A while loop repeats for as long as its condition remains true.</p>"
    "<img src=x onerror=alert(1)><a href='javascript:alert(2)'>bad</a>"
    "</body></html>"
).encode()

PLAIN_STATIC = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}


@override_settings(STORAGES=PLAIN_STATIC)
class BaseCase(TestCase):
    """Isolates note files in a temp dir and provides users, a level and helpers."""

    @classmethod
    def setUpTestData(cls):
        cls.trainer_group = Group.objects.get_or_create(name="Trainer")[0]
        cls.admin_group = Group.objects.get_or_create(name="Administrator")[0]
        cls.level = Trade.objects.create(key="l3", name="Level 3 — National IT", order=1)
        cls.alice = cls._user("alice", trainer=True)
        cls.bob = cls._user("bob", trainer=True)
        cls.admin = User.objects.create_superuser("root", "root@example.com", "pw-12345!")
        cls.student = cls._user("student", trainer=False)

    @staticmethod
    def _user(name, trainer):
        u = User.objects.create_user(name, f"{name}@example.com", "pw-12345!", first_name=name.title())
        if trainer:
            u.groups.add(Group.objects.get(name="Trainer"))
        return u

    def setUp(self):
        # Sandbox EVERY filesystem root the code can touch. The purge command
        # deletes files under MEDIA_ROOT and PRIVATE_MEDIA_ROOT, so tests must
        # never run against the real folders.
        self.tmp = tempfile.mkdtemp()            # private note storage
        self.media = tempfile.mkdtemp()          # MEDIA_ROOT
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.media, ignore_errors=True)
        ctx = self.settings(MEDIA_ROOT=self.media, PRIVATE_MEDIA_ROOT=self.tmp)
        ctx.enable()
        self.addCleanup(ctx.disable)
        field = ModuleNote._meta.get_field("file")
        patcher = mock.patch.object(field, "storage", FileSystemStorage(location=self.tmp))
        patcher.start()
        self.addCleanup(patcher.stop)

    # -- helpers ---------------------------------------------------------
    def files_on_disk(self):
        return [p for p in Path(self.tmp).rglob("*") if p.is_file()]

    def post_note(self, user, *, file=None, title="Loops notes", intent="publish", module_data=None, **extra):
        self.client.force_login(user)
        data = {"title": title, "intent": intent}
        data.update(module_data or {"new_module_code": "GENCP302",
                                    "new_module_name": "C Programming",
                                    "new_module_level": self.level.pk})
        data.update(extra)
        if file is not False:
            data["file"] = file or SimpleUploadedFile("notes.pdf", PDF_BYTES, "application/pdf")
        return self.client.post(reverse("core:note_create"), data)

    def make_note(self, user=None, published=True, kind="pdf"):
        self.post_note(user or self.alice, intent="publish" if published else "draft",
                       file=None if kind == "pdf" else SimpleUploadedFile("n.html", HTML_OK, "text/html"))
        return ModuleNote.objects.latest("pk")


# --------------------------------------------------------------------------- #
class AddingNotes(BaseCase):
    def test_trainer_publishes_pdf_and_new_module_is_created_and_assigned(self):
        r = self.post_note(self.alice)
        self.assertRedirects(r, reverse("core:notes_manage"))
        n = ModuleNote.objects.get()
        self.assertTrue(n.is_published)
        self.assertIsNotNone(n.published_at)
        self.assertEqual((n.file_type, n.title, n.uploaded_by), ("pdf", "Loops notes", self.alice))
        self.assertEqual((n.module.code, n.module.name, n.module.trade), ("GENCP302", "C Programming", self.level))
        self.assertIn(n.module, self.alice.profile.trainer_modules.all())
        self.assertEqual(len(self.files_on_disk()), 1)

    def test_save_as_draft_is_not_published(self):
        self.post_note(self.alice, intent="draft")
        n = ModuleNote.objects.get()
        self.assertFalse(n.is_published)
        self.assertIsNone(n.published_at)

    def test_unknown_intent_never_publishes(self):
        self.post_note(self.alice, intent="anything-else")
        self.assertFalse(ModuleNote.objects.get().is_published)

    def test_select_existing_assigned_module(self):
        self.post_note(self.alice)
        m = Module.objects.get()
        self.post_note(self.alice, title="Second", module_data={"module": m.pk})
        self.assertEqual(ModuleNote.objects.filter(module=m).count(), 2)
        self.assertEqual(Module.objects.count(), 1)

    def test_html_note_is_sanitised_and_indexed(self):
        self.post_note(self.alice, file=SimpleUploadedFile("loops.html", HTML_OK, "text/html"))
        n = ModuleNote.objects.get()
        self.assertEqual(n.file_type, "html")
        self.assertIn("for loop", n.content_html)
        for bad in ("<script", "onerror", "javascript:", "alert("):
            self.assertNotIn(bad, n.content_html)
        self.assertGreater(n.words, 15)
        self.assertGreaterEqual(len(n.headings_json), 2)

    def test_cannot_use_a_module_you_do_not_have(self):
        self.post_note(self.alice)
        m = Module.objects.get()
        r = self.post_note(self.bob, module_data={"module": m.pk})
        self.assertEqual(r.status_code, 200)                     # form re-rendered with an error
        self.assertEqual(ModuleNote.objects.count(), 1)

    def test_new_module_code_clash_is_explained(self):
        self.post_note(self.alice)
        r = self.post_note(self.bob)                             # same code + level, not assigned to bob
        self.assertContains(r, "isn&#x27;t assigned to you")
        self.assertEqual(ModuleNote.objects.count(), 1)
        r = self.post_note(self.alice)                           # alice already has it
        self.assertContains(r, "pick it from the Module list")

    def test_missing_module_and_missing_file_are_reported(self):
        r = self.post_note(self.alice, module_data={"title": "x"}, file=False)
        self.assertContains(r, "Select a module")
        self.assertContains(r, "Choose a PDF or HTML file")
        self.assertEqual(ModuleNote.objects.count(), 0)


class FileValidation(BaseCase):
    def _rejected(self, upload, message):
        r = self.post_note(self.alice, file=upload)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, message)
        self.assertEqual(ModuleNote.objects.count(), 0)
        self.assertEqual(self.files_on_disk(), [])

    def test_wrong_extension(self):
        self._rejected(SimpleUploadedFile("run.exe", b"MZ....", "application/octet-stream"),
                       "Upload a PDF")

    def test_html_disguised_as_pdf(self):
        self._rejected(SimpleUploadedFile("notes.pdf", HTML_OK, "application/pdf"),
                       "isn&#x27;t a valid PDF")

    def test_pdf_disguised_as_html(self):
        self._rejected(SimpleUploadedFile("notes.html", PDF_BYTES, "text/html"),
                       "This is a PDF with an .html file name")

    def test_empty_file(self):
        self._rejected(SimpleUploadedFile("notes.pdf", b"", "application/pdf"), "empty")

    def test_script_only_html_page(self):
        page = b"<html><body><script>" + b"var a=1;" * 600 + b"</script><p>hi</p></body></html>"
        self._rejected(SimpleUploadedFile("app.html", page, "text/html"), "interactive page")

    @override_settings(MAX_NOTE_SIZE_MB=1)
    def test_oversize(self):
        big = PDF_BYTES + b"0" * (1024 * 1024 + 10)
        self._rejected(SimpleUploadedFile("big.pdf", big, "application/pdf"), "the limit is 1 MB")


class Visibility(BaseCase):
    def test_published_note_is_public(self):
        n = self.make_note()
        self.client.logout()
        self.assertEqual(self.client.get(n.get_absolute_url()).status_code, 200)
        r = self.client.get(n.get_file_url())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Content-Type"], "application/pdf")
        self.assertEqual(r["X-Content-Type-Options"], "nosniff")
        self.assertTrue(r["Content-Disposition"].startswith("inline"))
        self.assertEqual(b"".join(r.streaming_content), PDF_BYTES)
        self.assertIn("Content-Length", r)

    def test_download_flag_forces_attachment(self):
        n = self.make_note()
        self.client.logout()
        r = self.client.get(n.get_download_url())
        self.assertTrue(r["Content-Disposition"].startswith("attachment"))

    def test_draft_is_hidden_from_public_and_other_trainers_but_not_author_or_admin(self):
        n = self.make_note(published=False)
        for who in (None, self.student, self.bob):
            self.client.logout()
            if who:
                self.client.force_login(who)
            self.assertEqual(self.client.get(n.get_absolute_url()).status_code, 404, who)
            self.assertEqual(self.client.get(n.get_file_url()).status_code, 404, who)
        for who in (self.alice, self.admin):
            self.client.force_login(who)
            self.assertEqual(self.client.get(n.get_absolute_url()).status_code, 200)
            self.assertEqual(self.client.get(n.get_file_url()).status_code, 200)

    def test_draft_cache_header_forbids_storing(self):
        n = self.make_note(published=False)
        self.client.force_login(self.alice)
        self.assertIn("no-store", self.client.get(n.get_file_url())["Cache-Control"])

    def test_html_note_is_only_ever_a_download_and_reader_has_csp(self):
        n = self.make_note(kind="html")
        self.client.logout()
        f = self.client.get(n.get_file_url())
        self.assertTrue(f["Content-Disposition"].startswith("attachment"))
        self.assertIn("sandbox", f["Content-Security-Policy"])
        page = self.client.get(n.get_absolute_url())
        csp = page["Content-Security-Policy"]
        self.assertIn("script-src 'self'", csp)
        self.assertNotIn("unsafe-inline'", csp.split("style-src")[0])
        self.assertNotIn(b"<script>alert", page.content)

    def test_files_are_not_reachable_under_media_url(self):
        n = self.make_note()
        name = Path(n.file.name).name
        self.assertEqual(self.client.get(f"/media/{n.file.name}").status_code, 404)
        self.assertNotIn(name, self.client.get(n.module.get_absolute_url()).content.decode())

    def test_missing_file_on_disk_is_a_clean_404(self):
        n = self.make_note()
        for p in self.files_on_disk():
            p.unlink()
        self.assertEqual(self.client.get(n.get_file_url()).status_code, 404)


class ManagingNotes(BaseCase):
    def test_anonymous_is_sent_to_sign_in_and_students_are_refused(self):
        for name in ("core:notes_manage", "core:note_create"):
            r = self.client.get(reverse(name))
            self.assertEqual(r.status_code, 302)
            self.assertIn("/accounts/login/", r["Location"])
        self.client.force_login(self.student)
        self.assertEqual(self.client.get(reverse("core:notes_manage")).status_code, 403)

    def test_trainer_sees_only_own_notes_admin_sees_all(self):
        a = self.make_note(self.alice)
        b = self.post_note(self.bob, title="Bob notes",
                           module_data={"new_module_code": "NITWA401", "new_module_name": "Servers",
                                        "new_module_level": self.level.pk}) and ModuleNote.objects.get(title="Bob notes")
        self.client.force_login(self.alice)
        page = self.client.get(reverse("core:notes_manage"))
        self.assertContains(page, a.title)
        self.assertNotContains(page, "Bob notes")
        self.client.force_login(self.admin)
        page = self.client.get(reverse("core:notes_manage"))
        self.assertContains(page, a.title)
        self.assertContains(page, "Bob notes")

    def test_only_author_or_admin_can_edit_publish_or_delete(self):
        n = self.make_note(self.alice)
        for name, method in (("core:note_edit", "get"), ("core:note_delete", "get"),
                             ("core:note_toggle_publish", "post"), ("core:note_delete", "post")):
            self.client.force_login(self.bob)
            r = getattr(self.client, method)(reverse(name, args=[n.pk]))
            self.assertEqual(r.status_code, 403, name)
        n.refresh_from_db()
        self.assertTrue(n.is_published)
        self.assertEqual(ModuleNote.objects.count(), 1)
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(reverse("core:note_edit", args=[n.pk])).status_code, 200)

    def test_unpublish_and_republish(self):
        n = self.make_note(self.alice)
        self.client.force_login(self.alice)
        self.client.post(reverse("core:note_toggle_publish", args=[n.pk]))
        n.refresh_from_db()
        self.assertFalse(n.is_published)
        self.client.logout()
        self.assertEqual(self.client.get(n.get_absolute_url()).status_code, 404)
        self.client.force_login(self.alice)
        self.client.post(reverse("core:note_toggle_publish", args=[n.pk]))
        n.refresh_from_db()
        self.assertTrue(n.is_published)

    def test_toggle_requires_post_and_ignores_offsite_next(self):
        n = self.make_note(self.alice)
        self.client.force_login(self.alice)
        self.assertEqual(self.client.get(reverse("core:note_toggle_publish", args=[n.pk])).status_code, 405)
        r = self.client.post(reverse("core:note_toggle_publish", args=[n.pk]), {"next": "https://evil.example/"})
        self.assertEqual(r["Location"], reverse("core:notes_manage"))

    def test_edit_title_and_module_keeps_file_and_state(self):
        n = self.make_note(self.alice)
        old_file = n.file.name
        self.client.force_login(self.alice)
        r = self.client.post(reverse("core:note_edit", args=[n.pk]), {
            "title": "  Renamed   notes ", "intent": "save",
            "new_module_code": "NITWA401", "new_module_name": "Servers", "new_module_level": self.level.pk})
        self.assertRedirects(r, reverse("core:notes_manage"))
        n.refresh_from_db()
        self.assertEqual((n.title, n.module.code, n.file.name, n.is_published),
                         ("Renamed notes", "NITWA401", old_file, True))

    def test_edit_intents_change_publish_state(self):
        n = self.make_note(self.alice)
        url = reverse("core:note_edit", args=[n.pk])
        base = {"title": n.title, "module": n.module_id}
        self.client.force_login(self.alice)
        self.client.post(url, {**base, "intent": "draft"})
        n.refresh_from_db(); self.assertFalse(n.is_published)
        self.client.post(url, {**base, "intent": "save"})
        n.refresh_from_db(); self.assertFalse(n.is_published)
        self.client.post(url, {**base, "intent": "publish"})
        n.refresh_from_db(); self.assertTrue(n.is_published)

    def test_replace_file_removes_the_old_one_and_can_change_type(self):
        n = self.make_note(self.alice)
        old = n.file.name
        self.client.force_login(self.alice)
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("core:note_edit", args=[n.pk]), {
                "title": n.title, "module": n.module_id, "intent": "save",
                "file": SimpleUploadedFile("new.html", HTML_OK, "text/html")})
        n.refresh_from_db()
        self.assertEqual(n.file_type, "html")
        self.assertIn("for loop", n.content_html)
        self.assertNotEqual(n.file.name, old)
        names = [p.name for p in self.files_on_disk()]
        self.assertEqual(len(names), 1)
        self.assertNotIn(Path(old).name, names)
        # …and back to a PDF clears the HTML-only fields
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("core:note_edit", args=[n.pk]), {
                "title": n.title, "module": n.module_id, "intent": "save",
                "file": SimpleUploadedFile("again.pdf", PDF_BYTES, "application/pdf")})
        n.refresh_from_db()
        self.assertEqual((n.file_type, n.content_html, n.words), ("pdf", "", 0))
        self.assertEqual(len(self.files_on_disk()), 1)

    def test_invalid_replacement_leaves_note_untouched(self):
        n = self.make_note(self.alice)
        old = n.file.name
        self.client.force_login(self.alice)
        r = self.client.post(reverse("core:note_edit", args=[n.pk]), {
            "title": n.title, "module": n.module_id, "intent": "save",
            "file": SimpleUploadedFile("bad.pdf", b"not a pdf", "application/pdf")})
        self.assertEqual(r.status_code, 200)
        n.refresh_from_db()
        self.assertEqual(n.file.name, old)
        self.assertEqual(len(self.files_on_disk()), 1)

    def test_delete_removes_row_and_file(self):
        n = self.make_note(self.alice)
        self.client.force_login(self.alice)
        self.assertEqual(self.client.get(reverse("core:note_delete", args=[n.pk])).status_code, 200)
        self.assertEqual(ModuleNote.objects.count(), 1)          # GET must not delete
        self.client.post(reverse("core:note_delete", args=[n.pk]))
        self.assertEqual(ModuleNote.objects.count(), 0)
        self.assertEqual(self.files_on_disk(), [])

    def test_deleting_a_module_removes_its_note_files(self):
        self.make_note(self.alice)
        Module.objects.get().delete()
        self.assertEqual(ModuleNote.objects.count(), 0)
        self.assertEqual(self.files_on_disk(), [])

    def test_removing_a_trainer_keeps_their_notes(self):
        n = self.make_note(self.alice)
        self.alice.delete()
        n.refresh_from_db()
        self.assertIsNone(n.uploaded_by)
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(reverse("core:note_edit", args=[n.pk])).status_code, 200)

    def test_filters_and_status_tabs(self):
        self.make_note(self.alice, published=True)
        self.post_note(self.alice, title="Hidden draft", intent="draft",
                       module_data={"module": Module.objects.get().pk})
        self.client.force_login(self.alice)
        url = reverse("core:notes_manage")
        self.client.get(url)     # consume the one-time "saved as draft" flash messages
        self.assertNotContains(self.client.get(url + "?status=published"), "Hidden draft")
        self.assertContains(self.client.get(url + "?status=draft"), "Hidden draft")
        self.assertContains(self.client.get(url + "?q=hidden"), "Hidden draft")
        self.assertNotContains(self.client.get(url + "?q=zzzz"), "Hidden draft")


class StudentFacing(BaseCase):
    def test_module_page_dashboard_and_search_show_published_only(self):
        pub = self.make_note(self.alice, published=True)
        self.post_note(self.alice, title="Secret draft", intent="draft",
                       module_data={"module": pub.module_id})
        self.client.logout()
        mod = self.client.get(pub.module.get_absolute_url())
        self.assertContains(mod, pub.title)
        self.assertNotContains(mod, "Secret draft")
        self.assertContains(self.client.get(reverse("core:dashboard")), pub.title)
        self.assertNotContains(self.client.get(reverse("core:dashboard")), "Secret draft")
        self.assertContains(self.client.get(reverse("core:search"), {"q": "loops"}), pub.title)
        self.assertNotContains(self.client.get(reverse("core:search"), {"q": "secret"}), "Secret draft")
        self.assertContains(self.client.get(reverse("core:browse")), "GENCP302")

    def test_module_with_only_drafts_is_not_listed_publicly(self):
        self.make_note(self.alice, published=False)
        self.client.logout()
        self.assertNotContains(self.client.get(reverse("core:browse")), "GENCP302")

    def test_html_note_body_is_searchable(self):
        self.post_note(self.alice, file=SimpleUploadedFile("l.html", HTML_OK, "text/html"))
        self.client.logout()
        self.assertContains(self.client.get(reverse("core:search"), {"q": "walking through arrays"}), "Loops notes")


# --------------------------------------------------------------------------- #
class PurgeCommand(BaseCase):
    def _seed(self):
        m = Module.objects.create(trade=self.level, key="GENCP302", code="GENCP302", name="C")
        u = Unit.objects.create(module=m, code="LO1", title="Intro")
        les = Lesson.objects.create(unit=u, title="L", content_html="<p>x</p>")
        Resource.objects.create(module=m, name="r", kind="pdf")
        StudentActivity.objects.create(user=self.alice, lesson=les)
        self.alice.profile.trainer_modules.add(m)
        self.post_note(self.alice, module_data={"module": m.pk})

    def test_dry_run_changes_nothing(self):
        self._seed()
        call_command("purge_module_data", "--dry-run", stdout=StringIO())
        self.assertTrue(Module.objects.exists())

    def test_requires_confirmation(self):
        self._seed()
        with mock.patch("builtins.input", return_value="no"), self.assertRaises(Exception):
            call_command("purge_module_data", stdout=StringIO())
        self.assertTrue(Module.objects.exists())

    def test_purge_empties_module_data_but_keeps_people_and_levels(self):
        self._seed()
        # imported / uploaded media that belongs to module data…
        for rel in ("legacy/L3/a.pdf", "resources/L3/b.pdf", "lesson_uploads/L3/c.html"):
            f = Path(self.media, rel); f.parent.mkdir(parents=True, exist_ok=True); f.write_bytes(b"x")
        # …and an unrelated file that must survive
        keep = Path(self.media, "branding/logo.png"); keep.parent.mkdir(parents=True); keep.write_bytes(b"x")
        call_command("purge_module_data", "--yes", stdout=StringIO())
        for gone in ("legacy", "resources", "lesson_uploads"):
            self.assertFalse(Path(self.media, gone).exists(), gone)
        self.assertTrue(keep.exists(), "purge must not touch unrelated media")
        for model in (Module, Unit, Lesson, Resource, StudentActivity, ModuleNote):
            self.assertEqual(model.objects.count(), 0, model.__name__)
        self.assertEqual(self.files_on_disk(), [])
        self.assertEqual(Trade.objects.count(), 1)
        self.assertTrue(User.objects.filter(username="alice").exists())
        self.assertEqual(self.alice.profile.trainer_modules.count(), 0)
        self.assertIn(self.trainer_group, self.alice.groups.all())
        call_command("purge_module_data", "--yes", stdout=StringIO())      # idempotent

    def test_include_levels_flag(self):
        call_command("purge_module_data", "--yes", "--include-levels", stdout=StringIO())
        self.assertEqual(Trade.objects.count(), 0)


# --------------------------------------------------------------------------- #
class Sanitiser(TestCase):
    BODY = "<p>" + "Real readable words for the notes. " * 6 + "</p>"

    def run_(self, extra):
        return process_lesson_html(f"<html><body><h1>T</h1>{self.BODY}{extra}<p>tail marker text</p></body></html>")["fragment"]

    def test_dangerous_constructs_are_removed(self):
        out = self.run_(
            '<object data="data:text/html;base64,AAAA"></object><form action="//evil"><input name=x>'
            '<button>go</button></form><a href="  JaVaScRiPt:alert(1)">a</a><a href="java&#9;script:alert(2)">b</a>'
            '<div srcdoc="<script>1</script>" onclick="alert(3)">d</div><svg><use href="data:text/html,x"/></svg>')
        for bad in ("<object", "<form", "<input", "<button", "javascript", "srcdoc", "onclick", "data:text/html"):
            self.assertNotIn(bad, out.lower(), bad)

    def test_content_after_embed_is_not_swallowed(self):
        self.assertIn("tail marker text", self.run_('<embed src="x.swf">'))

    def test_button_text_is_kept_but_the_button_is_not(self):
        out = self.run_('<h2><button class="accordion-button">Read error messages</button></h2>')
        self.assertIn("Read error messages", out)
        self.assertNotIn("<button", out)

    def test_source_classes_cannot_hide_content(self):
        """A Bootstrap accordion panel (class "collapse") must not vanish on our Bootstrap-styled site."""
        out = self.run_('<div class="accordion-collapse collapse" data-bs-parent="#x" id="p2">'
                        '<pre>hidden panel text</pre></div><p class="d-none modal">also visible</p>'
                        '<div class="note">a note</div>')
        self.assertIn("hidden panel text", out)
        for cls in ("collapse", "d-none", "modal", "accordion", "data-bs"):
            self.assertNotIn(cls, out, cls)
        self.assertIn("codeblock", out)          # classes the platform applies are kept
        self.assertIn("callout callout--note", out)

    def test_normal_content_is_preserved(self):
        out = self.run_('<a href="https://ok.example">l</a><table><tr><td>a</td></tr></table><pre>x = 1</pre>')
        for keep in ("https://ok.example", "<table", "<pre", "tail marker text"):
            self.assertIn(keep, out)


# --------------------------------------------------------------------------- #
class EmptyLibraryRenders(BaseCase):
    """Every page renders on a clean database, for each kind of visitor."""

    def test_pages_render_for_everyone(self):
        urls = [reverse("core:dashboard"), reverse("core:browse"), reverse("core:browse") + "?trade=l3",
                reverse("core:search"), reverse("core:search") + "?q=zzz", reverse("accounts:login")]
        for who in (None, self.alice, self.admin):
            self.client.logout()
            if who:
                self.client.force_login(who)
            for u in urls:
                r = self.client.get(u)
                self.assertEqual(r.status_code, 200, (who, u))
                html = r.content.decode()
                self.assertIn('data-bs-theme="light"', html)
                self.assertIn("css/platform.css", html)
                self.assertNotIn("solarized", html.lower())
                self.assertNotIn("cdn.jsdelivr", html)
                self.assertNotIn("fonts.googleapis", html)

    def test_trainer_and_admin_screens_render(self):
        for u in (reverse("core:notes_manage"), reverse("core:note_create"),
                  reverse("accounts:profile"), reverse("accounts:password_change")):
            self.client.force_login(self.alice)
            self.assertEqual(self.client.get(u).status_code, 200, u)
        for u in (reverse("accounts:manage_trainers"), reverse("accounts:create_trainer"),
                  reverse("accounts:edit_trainer_modules", args=[self.alice.pk])):
            self.client.force_login(self.admin)
            self.assertEqual(self.client.get(u).status_code, 200, u)

    def test_error_messages_use_bootstrap_danger_class(self):
        self.client.force_login(self.alice)
        r = self.client.post(reverse("core:note_toggle_publish", args=[999999]), follow=True)
        self.assertEqual(r.status_code, 404)
        from django.contrib import messages
        from django.test import RequestFactory
        self.assertEqual(messages.constants.DEFAULT_TAGS[messages.ERROR], "error")
        from django.conf import settings
        self.assertEqual(settings.MESSAGE_TAGS[messages.ERROR], "danger")


# --------------------------------------------------------------------------- #
class AdminIsThemed(BaseCase):
    """The Django admin picks up the platform theme and its key pages render."""

    def test_login_and_admin_pages_use_the_theme(self):
        r = self.client.get("/admin/login/")
        self.assertContains(r, "css/admin-theme.css")
        self.assertContains(r, "nit-mark")
        self.client.force_login(self.admin)
        for url in ("/admin/", "/admin/core/module/", "/admin/core/module/add/",
                    "/admin/core/modulenote/", "/admin/core/trade/", "/admin/auth/user/"):
            r = self.client.get(url)
            self.assertEqual(r.status_code, 200, url)
            self.assertContains(r, "css/admin-theme.css")
        self.assertContains(self.client.get("/admin/"), "NIT Learning Resources")
        self.assertContains(self.client.get("/admin/"), reverse("core:notes_manage"))

    def test_note_change_page_renders_without_exposing_a_public_file_link(self):
        n = self.make_note()
        self.client.force_login(self.admin)
        r = self.client.get(f"/admin/core/modulenote/{n.pk}/change/")
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, "/media/module_notes")


# --------------------------------------------------------------------------- #
class NotebookStyling(TestCase):
    """The handwriting/notebook look for module notes: fonts are bundled and the styling stays scoped."""

    CSS = Path(__file__).resolve().parent.parent / "static" / "css" / "platform.css"
    FONTS = Path(__file__).resolve().parent.parent / "static" / "vendor" / "handwriting"

    def test_every_font_file_referenced_by_the_css_ships_with_the_project(self):
        import re
        css = self.CSS.read_text()
        refs = set(re.findall(r'url\("\.\./vendor/handwriting/([^"]+)"\)', css))
        self.assertGreaterEqual(len(refs), 4)                        # Patrick Hand, Caveat, Courier Prime x2
        for name in refs:
            self.assertTrue((self.FONTS / name).is_file(), name)
        for family in ("Patrick Hand", "Caveat", "Courier Prime"):
            self.assertIn(f'font-family: "{family}"', css)
        self.assertTrue(list(self.FONTS.glob("LICENSE-*")), "font licences must ship with the fonts")

    def test_notebook_rules_do_not_leak_onto_lessons_or_the_interface(self):
        css = self.CSS.read_text()
        start = css.index("Notebook look")
        end = css.index("print -- */", start)          # the next section's header comment
        block = css[start:end]
        import re
        # every selector in the notebook section must be scoped to the note reader
        for selector in re.findall(r"^([^{}@/\n][^{}\n]*)\{", block, flags=re.M):
            for part in (p.strip() for p in selector.split(",")):
                if not part or part in ("from", "to"):
                    continue
                self.assertTrue(
                    part.startswith("#note-prose") or ".reader-card:has(> #note-prose)" in part
                    or part.startswith("main header.mb-4:not(.lesson-head)"),
                    f"unscoped selector in notebook section: {part!r}")
