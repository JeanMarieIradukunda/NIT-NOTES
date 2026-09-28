"""Tests for the Vercel Blob storage backend and PDF delivery through it.

Vercel Blob is emulated with a fake HTTP session that follows the real
protocol: uploads go to the Blob API with an access header; reads go to
https://<store-id>.<public|private>.blob.vercel-storage.com/<pathname>, with a
Bearer token required on private stores.
"""

import json
from unittest import mock
from urllib.parse import unquote, urlparse

from django.core.files.base import ContentFile
from django.test import SimpleTestCase, override_settings
from django.urls import reverse

from core import storage as blob
from core.models import ModuleNote
from core.storage import VercelBlobStorage
from core.tests import PDF_BYTES, BaseCase

TOKEN = "vercel_blob_rw_store1x_secretsecret"


class FakeResponse:
    def __init__(self, status=200, body=b"", json_body=None):
        self.status_code = status
        self.content = body if json_body is None else json.dumps(json_body).encode()
        self.text = self.content.decode("utf-8", "replace")
        self._json = json_body

    def json(self):
        return self._json


class FakeVercel:
    """A Blob store of the given access type ("public" or "private")."""

    def __init__(self, access, canonical_host=None, rename=None):
        self.access = access
        self.canonical_host = canonical_host   # if set, ONLY this host serves reads
        self.rename = rename or (lambda p: p)  # emulate the server altering pathnames
        self.blobs = {}
        self.calls = []

    # requests.Session.request
    def request(self, method, url, headers=None, params=None, data=None, json=None, **kw):
        headers = {k.lower(): v for k, v in (headers or {}).items()}
        self.calls.append((method, url, headers.get("x-vercel-blob-access")))
        host = urlparse(url).netloc
        if url.startswith(blob.DEFAULT_API_URL):
            if headers.get("authorization") != f"Bearer {TOKEN}":
                return FakeResponse(403, json_body={"error": {"code": "forbidden"}})
            if method == "PUT":
                if headers.get("x-vercel-blob-access") != self.access:
                    return FakeResponse(400, json_body={"error": {
                        "code": "bad_request", "message": f"This store is {self.access}"}})
                stored = self.rename(params["pathname"])
                self.blobs[stored] = data
                return FakeResponse(200, json_body={
                    "pathname": stored,
                    "url": f"https://store1x.{self.access}.blob.vercel-storage.com/{stored}"})
            if method == "GET":
                path = params["url"]
                if path not in self.blobs:
                    return FakeResponse(404, json_body={"error": {"code": "not_found"}})
                host_ = self.canonical_host or "store1x"
                return FakeResponse(200, json_body={
                    "size": len(self.blobs[path]),
                    "url": f"https://{host_}.{self.access}.blob.vercel-storage.com/{path}"})
            if method == "POST":
                for u in json["urls"]:
                    self.blobs.pop(urlparse(u).path.lstrip("/") if u.startswith("http") else u, None)
                return FakeResponse(200, json_body={})
        # CDN read
        if host.endswith(".blob.vercel-storage.com"):
            access = host.split(".")[1]
            path = unquote(urlparse(url).path.lstrip("/"))
            if access != self.access:
                return FakeResponse(404)
            if self.canonical_host and not host.startswith(self.canonical_host + "."):
                return FakeResponse(404)
            if access == "private" and headers.get("authorization") != f"Bearer {TOKEN}":
                return FakeResponse(403)
            if path in self.blobs:
                return FakeResponse(200, self.blobs[path])
            return FakeResponse(404)
        raise AssertionError(f"unexpected request {method} {url}")


def use_fake(testcase, access, **kwargs):
    fake = FakeVercel(access, **kwargs)
    p = mock.patch.object(blob, "_session", lambda: fake)
    p.start()
    testcase.addCleanup(p.stop)
    blob._learned_access.clear()
    testcase.addCleanup(blob._learned_access.clear)
    return fake


@override_settings(BLOB_READ_WRITE_TOKEN=TOKEN, BLOB_ACCESS="")
class BlobStorageProtocol(SimpleTestCase):
    def roundtrip(self, access):
        fake = use_fake(self, access)
        s = VercelBlobStorage(prefix="private_media")
        name = s.save("module_notes/l3/gencp302/abc.pdf", ContentFile(PDF_BYTES))
        self.assertEqual(name, "module_notes/l3/gencp302/abc.pdf")
        self.assertIn("private_media/module_notes/l3/gencp302/abc.pdf", fake.blobs)
        self.assertEqual(s.open(name).read(), PDF_BYTES)
        self.assertTrue(s.exists(name))
        self.assertEqual(s.size(name), len(PDF_BYTES))
        s.delete(name)
        self.assertFalse(fake.blobs)
        with self.assertRaises(FileNotFoundError):
            s.open(name)

    def test_public_store_auto_detected(self):
        self.roundtrip("public")

    def test_private_store_auto_detected(self):
        self.roundtrip("private")

    def test_old_api_host_is_never_used_for_reads(self):
        fake = use_fake(self, "public")
        s = VercelBlobStorage(prefix="private_media")
        s.save("a.pdf", ContentFile(PDF_BYTES))
        s.open("a.pdf")
        self.assertFalse([c for c in fake.calls if "//blob.vercel-storage.com" in c[1]])

    def test_falls_back_to_the_url_the_api_reports(self):
        # The regression from the field: uploads succeed, but the guessed read
        # address 404s. The backend must ask the API where the blob really is.
        use_fake(self, "public", canonical_host="store1x-canonical")
        s = VercelBlobStorage(prefix="private_media")
        name = s.save("a.pdf", ContentFile(PDF_BYTES))
        self.assertEqual(s.open(name).read(), PDF_BYTES)

    def test_stores_the_pathname_the_server_actually_used(self):
        use_fake(self, "public", rename=lambda p: p.replace(".pdf", "-x9.pdf"))
        s = VercelBlobStorage(prefix="private_media")
        name = s.save("dir/a.pdf", ContentFile(PDF_BYTES))
        self.assertEqual(name, "dir/a-x9.pdf")
        self.assertEqual(s.open(name).read(), PDF_BYTES)

    def test_store_id_is_lowercased_in_the_host(self):
        s = VercelBlobStorage(prefix="p", token="vercel_blob_rw_AbC123_secret")
        self.assertEqual(s._blob_url("a.pdf", "public"),
                         "https://abc123.public.blob.vercel-storage.com/p/a.pdf")

    def test_diagnose_never_leaks_the_token(self):
        use_fake(self, "private")
        s = VercelBlobStorage(prefix="p")
        s.save("a.pdf", ContentFile(b"%PDF-1"))
        report = "\n".join(s.diagnose("a.pdf"))
        self.assertNotIn("secretsecret", report)
        self.assertIn("HTTP 200", report)

    def test_token_is_only_sent_to_own_private_host(self):
        fake = use_fake(self, "public")
        sent = []
        orig = fake.request

        def spy(method, url, headers=None, **kw):
            if headers and any(k.lower() == "authorization" for k in headers):
                sent.append(urlparse(url).netloc)
            return orig(method, url, headers=headers, **kw)
        fake.request = spy
        s = VercelBlobStorage(prefix="p")
        s.save("a.pdf", ContentFile(b"%PDF-1"))
        s.open("a.pdf")
        for host in sent:
            self.assertIn(host, ("vercel.com",))  # never a public blob host

    @override_settings(BLOB_ACCESS="private")
    def test_explicit_access_does_not_probe_the_other_mode(self):
        fake = use_fake(self, "private")
        s = VercelBlobStorage(prefix="p")
        s.save("a.pdf", ContentFile(b"%PDF-1"))
        s.open("a.pdf")
        self.assertEqual({c[2] for c in fake.calls if c[0] == "PUT"}, {"private"})
        self.assertFalse([c for c in fake.calls if ".public." in c[1]])

    def test_bad_token_is_an_error_not_a_missing_file(self):
        use_fake(self, "private")
        with override_settings(BLOB_READ_WRITE_TOKEN="vercel_blob_rw_store1x_wrong"):
            s = VercelBlobStorage(prefix="p")
            with self.assertRaises(blob.VercelBlobError):
                s.open("a.pdf")

    def test_upload_error_is_raised_clearly(self):
        fake = use_fake(self, "public")
        fake.request = lambda *a, **k: FakeResponse(500, b"boom")
        with self.assertRaises(blob.VercelBlobError):
            VercelBlobStorage(prefix="p").save("a.pdf", ContentFile(b"%PDF-1"))


@override_settings(BLOB_READ_WRITE_TOKEN=TOKEN, BLOB_ACCESS="")
class PdfOpensThroughBlob(BaseCase):
    """The reported bug: a published PDF must actually open."""

    def setUp(self):
        super().setUp()
        self.fake = use_fake(self, "public")
        field = ModuleNote._meta.get_field("file")
        p = mock.patch.object(field, "storage", VercelBlobStorage(prefix="private_media"))
        p.start()
        self.addCleanup(p.stop)

    def test_published_pdf_opens_inline_and_page_allows_the_viewer(self):
        n = self.make_note()
        self.client.logout()
        r = self.client.get(n.get_file_url())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Content-Type"], "application/pdf")
        self.assertTrue(r["Content-Disposition"].startswith("inline"))
        self.assertEqual(b"".join(r.streaming_content), PDF_BYTES)
        page = self.client.get(n.get_absolute_url())
        self.assertIn("object-src 'self'", page["Content-Security-Policy"])
        self.assertIn("script-src 'self'", page["Content-Security-Policy"])

    def test_html_note_page_keeps_object_src_none(self):
        n = self.make_note(kind="html")
        page = self.client.get(n.get_absolute_url())
        self.assertIn("object-src 'none'", page["Content-Security-Policy"])

    def test_unpublished_pdf_is_still_hidden(self):
        n = self.make_note(published=False)
        self.client.logout()
        self.assertEqual(self.client.get(n.get_file_url()).status_code, 404)

    def test_missing_blob_is_a_clean_404(self):
        n = self.make_note()
        self.fake.blobs.clear()
        self.assertEqual(self.client.get(n.get_file_url()).status_code, 404)

    def test_store_outage_is_a_friendly_502_not_a_crash(self):
        n = self.make_note()
        self.fake.request = lambda *a, **k: FakeResponse(503)
        r = self.client.get(n.get_file_url())
        self.assertEqual(r.status_code, 502)
        # Django HTML-escapes the apostrophe in the rendered error page.
        self.assertContains(r, "can&#x27;t be opened right now", status_code=502)
