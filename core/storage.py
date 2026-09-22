"""
core.storage
============

A Vercel Blob-backed Django ``Storage``.

Why this exists: Vercel's Python Functions run on a read-only, ephemeral
filesystem — a file written by one request is not guaranteed to still be
there for the next one, even on the same instance, and is gone for good on
the next deploy. Django's default ``FileSystemStorage`` (perfectly fine
locally or on a traditional server with a persistent disk) silently loses
every uploaded module note, resource and lesson file if it runs unmodified
on Vercel. This module replaces it with Vercel Blob storage
(https://vercel.com/docs/vercel-blob) whenever a store is connected, and
changes nothing else about how the rest of the app talks to storage — every
view and model already goes through Django's storage API
(``file.storage.open()``, ``file.url``, ``default_storage`` and so on),
never a raw filesystem path, so swapping the backend here is enough.

Wiring, see ``nit_platform/settings.py`` and ``core/models.py``:

* ``STORAGES["default"]`` uses this class (prefixed ``media/``) for public
  files — Resource uploads, past papers, imported images, lesson source
  HTML — whenever ``BLOB_READ_WRITE_TOKEN`` is set.
* ``core.models.private_storage()`` uses this class (prefixed
  ``private_media/``) for Trainer-uploaded module notes, under the exact
  same condition. Notes stay just as access-controlled as before: the
  Blob store holds files under unguessable random names (see
  ``upload_note_path``) and this app *never* prints a note's Blob URL to a
  template — every note is served through the permission-checked
  ``core:note_file`` view, which streams the bytes through the server
  after re-checking published/author/admin on every request, exactly as
  it does for local disk storage.
* With no token set (plain local development, or a traditional server
  deployment that isn't using Vercel Blob), both storages fall back to
  ``FileSystemStorage`` exactly as before — nothing about that setup
  changes.

Connect a store from the Vercel dashboard (Storage -> Blob -> Connect to
Project) and Vercel injects ``BLOB_READ_WRITE_TOKEN`` automatically, the
same way it injects ``DATABASE_URL`` for a connected Postgres database.
"""

from __future__ import annotations

import mimetypes
from urllib.parse import quote

from django.conf import settings
from django.core.files import File
from django.core.files.base import ContentFile
from django.core.files.storage import Storage
from django.utils.deconstruct import deconstructible

BLOB_API_BASE = "https://blob.vercel-storage.com"
BLOB_API_VERSION = "7"


class VercelBlobError(OSError):
    """Raised when the Vercel Blob API can't be reached or refuses a request."""


def _require_requests():
    try:
        import requests  # noqa: PLC0415 (imported lazily so it's only needed when Blob is actually used)
    except ImportError as exc:  # pragma: no cover - guarded by requirements.txt
        raise VercelBlobError(
            "The 'requests' package is required for Vercel Blob storage. "
            "Run `pip install requests`."
        ) from exc
    return requests


@deconstructible
class VercelBlobStorage(Storage):
    """
    A minimal Django ``Storage`` over the Vercel Blob REST API — just what
    Django's file-handling machinery and this project's views actually use:
    saving, opening, checking existence/size, building a URL, and deleting.

    Every blob is uploaded WITHOUT Vercel's random-suffix option, so the
    pathname this project already generates (``upload_note_path``,
    ``upload_resource_path``, ...) *is* the blob's pathname — no extra
    lookup step is needed to turn a stored name back into a URL.
    """

    def __init__(self, prefix: str = "", token: str | None = None):
        self.prefix = prefix.strip("/")
        self._token = token  # resolved lazily so importing this module never requires the token to exist yet

    # -- internals --------------------------------------------------------- #

    @property
    def token(self) -> str:
        tok = self._token or getattr(settings, "BLOB_READ_WRITE_TOKEN", "")
        if not tok:
            raise VercelBlobError(
                "BLOB_READ_WRITE_TOKEN is not set, but Vercel Blob storage was "
                "selected. Connect a Blob store to the project in the Vercel "
                "dashboard (Storage -> Blob -> Connect to Project), or unset "
                "BLOB_READ_WRITE_TOKEN to fall back to local disk storage."
            )
        return tok

    def _headers(self, **extra) -> dict:
        headers = {"authorization": f"Bearer {self.token}", "x-api-version": BLOB_API_VERSION}
        headers.update(extra)
        return headers

    def _full_path(self, name: str) -> str:
        name = name.replace("\\", "/").lstrip("/")
        return f"{self.prefix}/{name}" if self.prefix else name

    def _blob_url(self, name: str) -> str:
        # Blobs live at a predictable URL (base + pathname) because uploads
        # always disable Vercel's random-suffix option below — no separate
        # "look up the URL for this pathname" call is needed.
        return f"{BLOB_API_BASE}/{quote(self._full_path(name))}"

    # -- Storage API --------------------------------------------------------- #

    def _open(self, name: str, mode: str = "rb") -> File:
        requests = _require_requests()
        try:
            resp = requests.get(self._blob_url(name), headers=self._headers(), timeout=30)
        except requests.RequestException as exc:
            raise VercelBlobError(f"Could not reach Vercel Blob for '{name}': {exc}") from exc
        if resp.status_code == 404:
            raise FileNotFoundError(name)
        if resp.status_code >= 400:
            raise VercelBlobError(f"Vercel Blob refused to serve '{name}' ({resp.status_code}).")
        return ContentFile(resp.content, name=name)

    def _save(self, name: str, content: File) -> str:
        requests = _require_requests()
        content.seek(0)
        data = content.read()
        content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        try:
            resp = requests.put(
                self._blob_url(name),
                headers=self._headers(**{
                    "content-type": content_type,
                    "x-content-type": content_type,
                    # We already generate collision-free / intentionally-fixed
                    # names (random UUIDs for notes, trade/module folders for
                    # resources); let Django's own get_available_name() decide
                    # whether a name needs de-duplicating instead of letting
                    # Vercel silently rename it.
                    "x-add-random-suffix": "0",
                }),
                data=data,
                timeout=120,
            )
        except requests.RequestException as exc:
            raise VercelBlobError(f"Could not upload '{name}' to Vercel Blob: {exc}") from exc
        if resp.status_code >= 400:
            raise VercelBlobError(
                f"Vercel Blob refused the upload of '{name}' ({resp.status_code}): {resp.text[:300]}")
        return name

    def exists(self, name: str) -> bool:
        requests = _require_requests()
        try:
            resp = requests.head(self._blob_url(name), headers=self._headers(), timeout=15)
        except requests.RequestException:
            # Fail "doesn't exist" rather than crashing get_available_name();
            # the subsequent _save() will surface a clear error if the store
            # is genuinely unreachable.
            return False
        return resp.status_code == 200

    def size(self, name: str) -> int:
        requests = _require_requests()
        resp = requests.head(self._blob_url(name), headers=self._headers(), timeout=15)
        if resp.status_code >= 400:
            raise FileNotFoundError(name)
        return int(resp.headers.get("content-length") or 0)

    def url(self, name: str) -> str:
        return self._blob_url(name)

    def delete(self, name: str) -> None:
        requests = _require_requests()
        try:
            requests.post(
                f"{BLOB_API_BASE}/delete",
                headers=self._headers(**{"content-type": "application/json"}),
                json={"urls": [self._blob_url(name)]},
                timeout=30,
            )
        except requests.RequestException:
            # Deletion is best-effort on a post_delete signal — the database
            # row is already gone either way, and a stray blob under a
            # random/scoped name isn't reachable through the app.
            pass

    def get_accessed_time(self, name):  # pragma: no cover - not tracked by Blob
        raise NotImplementedError("VercelBlobStorage doesn't record access times.")

    def get_created_time(self, name):  # pragma: no cover - not exposed by the Blob API
        raise NotImplementedError("VercelBlobStorage doesn't expose creation times.")
