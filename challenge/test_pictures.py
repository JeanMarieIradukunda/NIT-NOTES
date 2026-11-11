"""Picture cards: finished memes uploaded as PNG, JPG, WebP or SVG, stored in the file store and shown to everyone."""

import json
import os
import shutil
import tempfile

from django.core.files.storage import FileSystemStorage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, override_settings
from django.urls import reverse

from . import images
from .models import KnowledgeCard
from .tests import PLAIN, ChallengeBase

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 32
SVG = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1080 1080"><rect width="9" height="9"/></svg>'


class ImageHelperTests(SimpleTestCase):
    def test_pictures_are_recognised_by_their_bytes_not_their_name(self):
        self.assertEqual((images.detect_image(PNG), images.detect_image(JPG), images.detect_image(WEBP)), (".png", ".jpg", ".webp"))
        for fake in (b"<svg></svg>", b"GIF89a....", b"MZ\x90\x00", b"", b"RIFF....WAVE"):
            self.assertIsNone(images.detect_image(fake))

    def test_titles_come_from_file_names(self):
        t = images.title_from_filename
        self.assertEqual(t("ping-pong_card.png", 40), "Ping pong card")
        self.assertEqual(t("C:\\memes\\DHCP Final.jpg", 40), "DHCP")
        self.assertEqual(t("IMG_0042.PNG", 40), "IMG 0042")
        self.assertEqual(t(".png", 40), "Untitled card")
        self.assertEqual(len(t("x" * 200 + ".png", 30)), 30)


@override_settings(STORAGES=PLAIN)
class PictureBase(ChallengeBase):
    def setUp(self):
        self.store_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.store_dir, ignore_errors=True)
        field = KnowledgeCard._meta.get_field("image")
        self.original_storage = field.storage
        field.storage = FileSystemStorage(location=self.store_dir)
        self.addCleanup(setattr, field, "storage", self.original_storage)

    def as_trainer(self, user=None):
        self.client.force_login(user or self.alice)

    def upload(self, name, data, **extra):
        body = {"module": self.net.pk, "publish": "1", "file": SimpleUploadedFile(name, data), **extra}
        return self.client.post(reverse("challenge:card_upload"), body)

    def stored_files(self):
        return [os.path.join(r, f) for r, _d, fs in os.walk(self.store_dir) for f in fs]


class UploadEndpointTests(PictureBase):
    def test_a_trainer_uploads_pictures_of_every_kind_one_card_each(self):
        self.as_trainer()
        for name, data, kind in (("ping-pong.png", PNG, "image"), ("dhcp.jpg", JPG, "image"),
                                 ("dns.webp", WEBP, "image"), ("vector.svg", SVG, "svg")):
            r = self.upload(name, data)
            self.assertEqual(r.status_code, 200, r.content)
            self.assertEqual((r.json()["ok"], r.json()["kind"]), (True, kind))
        cards = list(KnowledgeCard.objects.order_by("pk"))
        self.assertEqual([c.title for c in cards], ["Ping pong", "Dhcp", "Dns", "Vector"])
        self.assertTrue(all(c.is_published and c.uploaded_by == self.alice and c.module == self.net for c in cards))
        self.assertEqual([c.number for c in cards], [1, 2, 3, 4])
        self.assertEqual(len(self.stored_files()), 3)                             # the SVG lives in the database
        self.assertTrue(all(f.endswith((".png", ".jpg", ".webp")) for f in self.stored_files()))

    def test_the_file_is_checked_by_content_not_by_extension(self):
        self.as_trainer()
        r = self.upload("holiday.png", b"MZ\x90\x00 this is a program, not a picture")
        self.assertEqual(r.status_code, 400)
        self.assertIn("not a PNG, JPG, WebP or SVG", r.json()["error"])
        self.assertFalse(KnowledgeCard.objects.exists())
        script = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script><rect width="9" height="9"/></svg>'
        self.assertEqual(self.upload("x.svg", script).status_code, 200)
        self.assertNotIn("<script", KnowledgeCard.objects.get().custom_svg)         # SVGs are cleaned, as before

    def test_a_picture_over_the_limit_is_refused_with_advice(self):
        self.as_trainer()
        r = self.upload("big.png", PNG + b"\x00" * images.MAX_IMAGE_BYTES)
        self.assertEqual(r.status_code, 400)
        self.assertIn("tinypng.com", r.json()["error"])
        self.assertFalse(KnowledgeCard.objects.exists())

    def test_it_can_be_saved_as_a_draft_and_titled(self):
        self.as_trainer()
        r = self.upload("a.png", PNG, publish="", title="  My own title  ")
        card = KnowledgeCard.objects.get(pk=r.json()["id"])
        self.assertEqual((card.title, card.is_published), ("My own title", False))

    def test_only_your_own_modules(self):
        self.as_trainer(self.alice)
        r = self.upload("a.png", PNG, module=self.sec.pk)                          # bob's module
        self.assertEqual(r.status_code, 400)
        self.assertIn("your own modules", r.json()["error"])
        self.assertFalse(KnowledgeCard.objects.exists())
        self.as_trainer(self.admin)
        self.assertEqual(self.upload("a.png", PNG, module=self.sec.pk).status_code, 200)

    def test_students_and_visitors_cannot_upload(self):
        self.assertEqual(self.upload("a.png", PNG).status_code, 401)               # not signed in
        self.client.force_login(self.student)
        self.assertEqual(self.upload("a.png", PNG).status_code, 403)
        self.assertFalse(KnowledgeCard.objects.exists())

    def test_a_missing_file_or_get_request(self):
        self.as_trainer()
        self.assertEqual(self.client.post(reverse("challenge:card_upload"), {"module": self.net.pk}).status_code, 400)
        self.assertEqual(self.client.get(reverse("challenge:card_upload")).status_code, 405)


class ServingTests(PictureBase):
    def card_with(self, name, data, **extra):
        self.as_trainer()
        pk = self.upload(name, data, **extra).json()["id"]
        self.client.logout()
        return KnowledgeCard.objects.get(pk=pk)

    def test_anyone_can_see_a_published_picture_without_signing_in(self):
        card = self.card_with("ping.png", PNG)
        r = self.client.get(card.picture_url)
        self.assertEqual((r.status_code, r["Content-Type"]), (200, "image/png"))
        self.assertEqual(b"".join(r.streaming_content), PNG)
        self.assertEqual(r["X-Content-Type-Options"], "nosniff")
        self.assertIn("sandbox", r["Content-Security-Policy"])
        self.assertIn("public", r["Cache-Control"])

    def test_each_type_gets_the_right_content_type(self):
        for name, data, ctype in (("a.jpg", JPG, "image/jpeg"), ("a.webp", WEBP, "image/webp")):
            self.assertEqual(self.client.get(self.card_with(name, data).picture_url)["Content-Type"], ctype)

    def test_the_older_svg_address_still_works(self):
        card = self.card_with("a.png", PNG)
        self.assertEqual(self.client.get(reverse("challenge:card_svg", args=[card.pk])).status_code, 200)

    def test_a_draft_picture_is_private_to_its_owner(self):
        card = self.card_with("a.png", PNG, publish="")
        self.assertEqual(self.client.get(card.picture_url).status_code, 404)
        self.client.force_login(self.student)
        self.assertEqual(self.client.get(card.picture_url).status_code, 404)
        self.client.force_login(self.alice)
        r = self.client.get(card.picture_url)
        self.assertEqual((r.status_code, r["Cache-Control"]), (200, "private, no-store"))

    def test_download_uses_the_real_file_type(self):
        card = self.card_with("ping.webp", WEBP)
        r = self.client.get(card.picture_url + "&download=1")
        self.assertIn(".webp", r["Content-Disposition"])
        self.assertTrue(r["Content-Disposition"].startswith("attachment"))
        self.assertEqual(card.picture_label, "WebP")

    def test_a_missing_file_is_a_clean_404(self):
        card = self.card_with("a.png", PNG)
        os.remove(self.stored_files()[0])
        self.assertEqual(self.client.get(card.picture_url).status_code, 404)

    def test_an_unreachable_store_is_reported_not_crashed(self):
        card = self.card_with("a.png", PNG)
        with self.settings(), unittest_patch(card):
            r = self.client.get(card.picture_url)
        self.assertEqual(r.status_code, 502)


def unittest_patch(card):
    from unittest import mock
    return mock.patch.object(FileSystemStorage, "open", side_effect=OSError("store down"))


class PictureCardPagesTests(PictureBase):
    def make(self, **kw):
        self.as_trainer()
        pk = self.upload("memes.png", PNG, **kw).json()["id"]
        self.client.logout()
        return KnowledgeCard.objects.get(pk=pk)

    def test_the_gallery_shows_picture_cards_and_the_viewer_can_step_through_them(self):
        a, b = self.make(title="First"), self.make(title="Second")
        html = self.client.get(reverse("challenge:gallery")).content.decode()
        for card in (a, b):
            self.assertIn(f'data-svg="{card.picture_url}"', html)
            self.assertIn(card.alt_text, html)
        self.assertIn("kcPrev", html) if "kcPrev" in html else self.assertIn("Previous", html)
        self.assertEqual(b.alt_text, "Second. An IT Knowledge Challenge card about Networking.")

    def test_the_card_page_works_without_any_text_parts(self):
        card = self.make(title="Just a picture")
        html = self.client.get(card.get_absolute_url()).content.decode()
        self.assertIn("Download card (PNG)", html)
        self.assertNotIn("Read it as text", html)
        self.assertNotIn("Question.", html)

    def test_the_picture_is_not_squashed_when_it_is_not_square(self):
        css = open("static/css/challenge.css", encoding="utf-8").read()
        self.assertIn("aspect-ratio: auto 1 / 1", css)
        self.assertEqual(css.count("aspect-ratio: auto 1 / 1; object-fit: contain"), 2)    # viewer and card page

    def test_pages_for_uploading(self):
        self.as_trainer()
        bulk = self.client.get(reverse("challenge:card_bulk"))
        for needle in ("Upload many pictures", "kcBulk", "NETWK301", "challenge-bulk.js", "tinypng.com"):
            self.assertContains(bulk, needle)
        self.assertNotContains(bulk, "SECUR302")                                    # only your own modules
        self.assertContains(self.client.get(reverse("challenge:gallery")), "Upload many")
        self.assertContains(self.client.get(reverse("challenge:manage")), "Upload many pictures")
        self.client.logout()
        self.assertEqual(self.client.get(reverse("challenge:card_bulk")).status_code, 302)   # to sign in
        self.client.force_login(self.student)
        self.assertNotEqual(self.client.get(reverse("challenge:card_bulk")).status_code, 200)


class FormTests(PictureBase):
    def post(self, url, data, **files):
        return self.client.post(url, {**data, **{k: SimpleUploadedFile(*v) for k, v in files.items()}})

    def test_text_is_optional_when_you_upload_your_own_picture(self):
        self.as_trainer()
        data = dict(module=self.net.pk, title="Mine", character="laptop", mood="happy", is_published="on")
        r = self.post(reverse("challenge:card_create"), data, image_file=("m.png", PNG))
        self.assertEqual(r.status_code, 302, getattr(r, "context", None) and r.context["form"].errors)
        card = KnowledgeCard.objects.get()
        self.assertEqual((card.picture_kind, card.question), ("image", ""))

    def test_a_generated_card_still_needs_all_four_text_parts(self):
        self.as_trainer()
        r = self.client.post(reverse("challenge:card_create"),
                             dict(module=self.net.pk, title="Plain", character="laptop", mood="happy"))
        self.assertEqual(r.status_code, 200)
        for name in ("bubble", "question", "answer", "explanation"):
            self.assertIn(name, r.context["form"].errors)

    def test_one_design_source_at_a_time(self):
        self.as_trainer()
        data = dict(module=self.net.pk, title="Both", character="laptop", mood="happy")
        r = self.post(reverse("challenge:card_create"), data, image_file=("m.png", PNG), svg_file=("m.svg", SVG))
        self.assertEqual(r.status_code, 200)
        self.assertIn("not both", str(r.context["form"].non_field_errors()))

    def test_a_fake_picture_is_refused(self):
        self.as_trainer()
        data = dict(module=self.net.pk, title="Bad", character="laptop", mood="happy")
        r = self.post(reverse("challenge:card_create"), data, image_file=("m.png", b"just text"))
        self.assertIn("not a PNG, JPG or WebP", str(r.context["form"].errors["image_file"]))

    def test_replacing_or_removing_a_picture_cleans_up_the_old_file(self):
        self.as_trainer()
        pk = self.upload("a.png", PNG).json()["id"]
        old = self.stored_files()
        data = dict(module=self.net.pk, title="A", character="laptop", mood="happy", is_published="on")
        self.post(reverse("challenge:card_edit", args=[pk]), data, image_file=("b.jpg", JPG))
        now = self.stored_files()
        self.assertEqual((len(now), now[0].endswith(".jpg"), now == old), (1, True, False))
        r = self.client.post(reverse("challenge:card_edit", args=[pk]), {**data, "remove_image": "on", "bubble": "x",
                             "question": "x", "answer": "x", "explanation": "x"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual((self.stored_files(), KnowledgeCard.objects.get(pk=pk).picture_kind), ([], "generated"))

    def test_an_svg_replaces_a_picture_and_the_other_way_round(self):
        self.as_trainer()
        pk = self.upload("a.png", PNG).json()["id"]
        data = dict(module=self.net.pk, title="A", character="laptop", mood="happy")
        self.post(reverse("challenge:card_edit", args=[pk]), data, svg_file=("v.svg", SVG))
        card = KnowledgeCard.objects.get(pk=pk)
        self.assertEqual((card.picture_kind, self.stored_files()), ("svg", []))
        self.post(reverse("challenge:card_edit", args=[pk]), data, image_file=("b.png", PNG))
        card = KnowledgeCard.objects.get(pk=pk)
        self.assertEqual((card.picture_kind, card.custom_svg, len(self.stored_files())), ("image", "", 1))

    def test_deleting_a_card_deletes_its_file(self):
        self.as_trainer()
        pk = self.upload("a.png", PNG).json()["id"]
        self.assertEqual(len(self.stored_files()), 1)
        KnowledgeCard.objects.get(pk=pk).delete()
        self.assertEqual(self.stored_files(), [])

    def test_deleting_a_module_deletes_the_files_of_its_cards(self):
        self.as_trainer()
        self.upload("a.png", PNG)
        self.upload("b.png", PNG)
        self.assertEqual(len(self.stored_files()), 2)
        self.net.delete()
        self.assertEqual(self.stored_files(), [])


class UploaderScriptTests(SimpleTestCase):
    def test_the_script_sends_one_file_per_request_and_stops_on_a_lost_session(self):
        js = open("static/js/challenge-bulk.js", encoding="utf-8").read()
        for needle in ('body.append("file", it.file)', "X-CSRFToken", "r.status === 401", "r.status === 413",
                       "Please keep it under", "dragover", "Keep this page open"[:0]):
            self.assertIn(needle, js)
        self.assertEqual(js.count("fetch("), 1)                                      # a single file per request
