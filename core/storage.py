"""
core.storage
============

A Vercel Blob-backed Django ``Storage``.

Why this exists: Vercel's Python Functions run on a read-only, ephemeral
filesystem, so uploaded module notes, resources and lesson files must live in
Vercel Blob (https://vercel.com/docs/vercel-blob) when the app is deployed
there. This backend is used whenever ``BLOB_READ_WRITE_TOKEN`` is set; without
it the app falls back to plain local-disk storage (see ``nit_platform/settings.py``
and ``core.models.private_storage``).

How Vercel Blob really works (this is what the earlier version of this module
got wrong, which is why published PDFs would not open):

* **Writing** goes to the Blob API (``https://vercel.com/api/blob?pathname=...``)
  with the ``x-vercel-blob-access`` header set to ``public`` or ``private`` to
  match the store.
* **Reading** never goes through the API host. Every blob lives at
  ``https://<store-id>.<public|private>.blob.vercel-storage.com/<pathname>``.
  Public stores are readable by anyone with the URL; private stores need
  ``Authorization: Bearer <BLOB_READ_WRITE_TOKEN>``. The store id is the fourth
  ``_``-separated part of the token (``vercel_blob_rw_<store-id>_<secret>``).

The store decides whether its blobs are public or private, and this project
supports both. Set ``BLOB_ACCESS=public`` or ``BLOB_ACCESS=private`` to be
explicit, or leave it unset (``auto``) and the backend works it out on first
use and remembers the answer for the life of the process.

Module notes stay access-controlled either way: this app never prints a note's
Blob URL into a page. Files are only delivered through the permission-checked
``core:note_file`` view, which streams the bytes through the server after
re-checking published/author/admin on every request. (For real protection
against someone who is handed a URL, use a *private* store.)

Verify a deployment with:  ``python manage.py check_blob``
"""

from __future__ import annotations

import logging
import mimetypes
import os
import time
import uuid
from urllib.parse import quote, urlparse

from django.conf import settings
from django.core.files import File
from django.core.files.base import ContentFile
from django.core.files.storage import Storage
from django.utils.deconstruct import deconstructible

logger = logging.getLogger(__name__)

DEFAULT_API_URL = "https://vercel.com/api/blob"
BLOB_API_VERSION = "11"
ACCESS_MODES = ("public", "private")

# Learned per process: store id -> "public" | "private".
_learned_access: dict[str, str] = {}


class VercelBlobError(OSError):
    """Raised when the Vercel Blob service can't be reached or refuses a request."""


def _requests():
    try:
        import requests  # noqa: PLC0415 (only needed when Blob is actually used)
    except ImportError as exc:  # pragma: no cover - guarded by requirements.txt
        raise VercelBlobError(
            "The 'requests' package is required for Vercel Blob storage. "
            "Run `pip install requests`.") from exc
    return requests


def _session():
    """One place to build the HTTP session (tests replace this)."""
    return _requests().Session()


def store_id_from_token(token: str) -> str:
    """``vercel_blob_rw_<store-id>_<secret>`` -> ``<store-id>`` (falls back to BLOB_STORE_ID)."""
    parts = (token or "").split("_")
    if len(parts) > 3 and parts[3]:
        return parts[3]
    env = os.environ.get("BLOB_STORE_ID", "").strip()
    return env[len("store_"):] if env.startswith("store_") else env


def configured_access() -> str:
    value = (getattr(settings, "BLOB_ACCESS", "") or os.environ.get("BLOB_ACCESS", "")
             or "auto").strip().lower()
    return value if value in ACCESS_MODES else "auto"


def _snippet(resp) -> str:
    try:
        return (resp.text or "")[:200].replace("\n", " ")
    except Exception:  # pragma: no cover
        return ""


@deconstructible
class VercelBlobStorage(Storage):
    """
    A minimal Django ``Storage`` over Vercel Blob: save, open, exists, size,
    url and delete. Uploads never use Vercel's random suffix, so the pathname
    this project generates (``upload_note_path`` etc.) *is* the blob pathname.
    """

    def __init__(self, prefix: str = "", token: str | None = None, access: str | None = None):
        self.prefix = prefix.strip("/")
        self._token = token
        self._access = access  # explicit override; None -> BLOB_ACCESS / auto

    # -- configuration ----------------------------------------------------- #

    @property
    def token(self) -> str:
        tok = self._token or getattr(settings, "BLOB_READ_WRITE_TOKEN", "") \
            or os.environ.get("BLOB_READ_WRITE_TOKEN", "")
        if not tok:
            raise VercelBlobError(
                "BLOB_READ_WRITE_TOKEN is not set, but Vercel Blob storage was selected. "
                "Connect a Blob store to the project in the Vercel dashboard "
                "(Storage -> Blob -> Connect to Project), or unset it to use local disk.")
        return tok

    @property
    def store_id(self) -> str:
        return store_id_from_token(self.token)

    def _api_url(self, suffix: str = "") -> str:
        base = os.environ.get("VERCEL_BLOB_API_URL") or DEFAULT_API_URL
        return f"{base}{suffix}"

    def _api_headers(self, **extra) -> dict:
        headers = {
            "authorization": f"Bearer {self.token}",
            "x-api-version": os.environ.get("VERCEL_BLOB_API_VERSION_OVERRIDE") or BLOB_API_VERSION,
            "x-api-blob-request-id": f"{self.store_id}:{int(time.time() * 1000)}:{uuid.uuid4().hex[:8]}",
        }
        headers.update(extra)
        return headers

    def _full_path(self, name: str) -> str:
        name = name.replace("\\", "/").lstrip("/")
        return f"{self.prefix}/{name}" if self.prefix else name

    def _access_order(self) -> list[str]:
        explicit = self._access or configured_access()
        if explicit in ACCESS_MODES:
            return [explicit]
        learned = _learned_access.get(self.store_id)
        if learned in ACCESS_MODES:
            return [learned] + [m for m in ACCESS_MODES if m != learned]
        return list(ACCESS_MODES)  # public first: sends no credentials on the first try

    def _known_access(self) -> str | None:
        explicit = self._access or configured_access()
        if explicit in ACCESS_MODES:
            return explicit
        return _learned_access.get(self.store_id)

    def _remember(self, access: str) -> None:
        if (self._access or configured_access()) == "auto":
            _learned_access[self.store_id] = access

    def _blob_url(self, name: str, access: str) -> str:
        # Blob hostnames are always lowercase, but the token's store id isn't.
        return (f"https://{self.store_id.lower()}.{access}.blob.vercel-storage.com/"
                f"{quote(self._full_path(name))}")

    def _send(self, method: str, url: str, *, retries: int = 0, **kwargs):
        """HTTP with a short retry on connection errors / 5xx (reads only)."""
        requests = _requests()
        session = _session()
        attempt = 0
        while True:
            try:
                resp = session.request(method, url, **kwargs)
            except requests.RequestException as exc:
                if attempt < retries:
                    attempt += 1
                    time.sleep(0.3 * attempt)
                    continue
                raise VercelBlobError(f"Could not reach Vercel Blob ({method}): {exc}") from exc
            if resp.status_code >= 500 and attempt < retries:
                attempt += 1
                time.sleep(0.3 * attempt)
                continue
            return resp

    # -- Storage API --------------------------------------------------------- #

    @staticmethod
    def _is_blob_host(url: str) -> bool:
        parsed = urlparse(url)
        return parsed.scheme == "https" and (parsed.hostname or "").endswith(".blob.vercel-storage.com")

    def _open_via_head(self, name: str) -> File:
        """Ask the Blob API where the file really is, then fetch that exact URL."""
        meta = self._head(name)
        url = (meta or {}).get("url")
        if not url:
            raise FileNotFoundError(name)
        private = ".private." in url
        # Only ever send the token to Vercel's own blob hosts.
        headers = {"authorization": f"Bearer {self.token}"} if (private and self._is_blob_host(url)) else {}
        resp = self._send("GET", url, headers=headers, timeout=30, retries=2)
        if resp.status_code == 200:
            self._remember("private" if private else "public")
            return ContentFile(resp.content, name=name)
        if resp.status_code == 404:
            raise FileNotFoundError(name)
        raise VercelBlobError(f"Vercel Blob returned {resp.status_code} for '{name}'.")

    def _open(self, name: str, mode: str = "rb") -> File:
        if not self.store_id:
            # Can't build the store URL from the token: ask the API for it.
            return self._open_via_head(name)

        statuses = []
        for access in self._access_order():
            url = self._blob_url(name, access)
            # The token is only ever sent to this store's private host.
            headers = {"authorization": f"Bearer {self.token}"} if access == "private" else {}
            resp = self._send("GET", url, headers=headers, timeout=30, retries=2)
            if resp.status_code == 200:
                self._remember(access)
                return ContentFile(resp.content, name=name)
            statuses.append(resp.status_code)
            logger.info("Vercel Blob GET %s -> %s", url, resp.status_code)
            if resp.status_code not in (401, 403, 404):
                break

        if any(s >= 500 or s not in (401, 403, 404) for s in statuses):
            raise VercelBlobError(f"Vercel Blob returned {statuses} for '{name}'.")
        if 401 in statuses or 403 in statuses:
            # A 401/403 means we reached a store of the right access type but
            # were rejected by it: a credentials problem, not a missing file.
            # (A 404 on the *other* access type just means "wrong host".)
            raise VercelBlobError(
                f"Vercel Blob refused access to '{name}' ({statuses}). Check "
                "BLOB_READ_WRITE_TOKEN and BLOB_ACCESS.")
        # Every guessed address was a 404. Don't trust the guess: ask the API
        # where the blob actually lives (None -> genuinely missing).
        return self._open_via_head(name)

    def diagnose(self, name: str) -> list[str]:
        """Human-readable request-level report for `manage.py check_blob`. Never prints the token."""
        path = self._full_path(name)
        lines = [f"store id : {self.store_id or '(not derivable from token)'}",
                 f"pathname : {path}"]
        canonical = None
        try:
            resp = self._send("GET", self._api_url(), params={"url": path},
                              headers=self._api_headers(), timeout=15)
            lines.append(f"API head : HTTP {resp.status_code} {_snippet(resp)}")
            if resp.status_code == 200:
                canonical = resp.json().get("url")
        except (OSError, ValueError) as exc:
            lines.append(f"API head : failed ({exc})")
        if self.store_id:
            for access in ACCESS_MODES:
                url = self._blob_url(name, access)
                headers = {"authorization": f"Bearer {self.token}"} if access == "private" else {}
                try:
                    lines.append(f"GET {url} -> HTTP {self._send('GET', url, headers=headers, timeout=15).status_code}")
                except OSError as exc:
                    lines.append(f"GET {url} -> failed ({exc})")
        if canonical:
            headers = {"authorization": f"Bearer {self.token}"} \
                if (".private." in canonical and self._is_blob_host(canonical)) else {}
            try:
                lines.append(f"GET {canonical} (URL reported by API) -> "
                             f"HTTP {self._send('GET', canonical, headers=headers, timeout=15).status_code}")
            except OSError as exc:
                lines.append(f"GET {canonical} -> failed ({exc})")
        return lines

    def _save(self, name: str, content: File) -> str:
        content.seek(0)
        data = content.read()
        content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        order = self._access_order()
        first_error = None

        for i, access in enumerate(order):
            resp = self._send(
                "PUT", self._api_url(), params={"pathname": self._full_path(name)},
                headers=self._api_headers(**{
                    "x-content-type": content_type,
                    "x-vercel-blob-access": access,
                    # Names are already collision-free; Django's get_available_name()
                    # decides whether to rename, not Vercel.
                    "x-add-random-suffix": "0",
                    "x-allow-overwrite": "0",
                }),
                data=data, timeout=120)
            if 200 <= resp.status_code < 300:
                self._remember(access)
                try:
                    body = resp.json() or {}
                except ValueError:
                    body = {}
                self.last_put = {"access": access, "url": body.get("url"),
                                 "pathname": body.get("pathname")}
                # Store whatever pathname Blob says it used (minus our prefix), so
                # the database always points at the file that really exists.
                returned = body.get("pathname")
                lead = f"{self.prefix}/" if self.prefix else ""
                if isinstance(returned, str) and returned.startswith(lead) and returned[len(lead):]:
                    if returned[len(lead):] != name:
                        logger.warning("Vercel Blob stored '%s' as '%s'", name, returned)
                    return returned[len(lead):]
                return name
            error = f"Vercel Blob refused the upload of '{name}' ({resp.status_code}): {_snippet(resp)}"
            first_error = first_error or error
            # A 400/403 may just mean "this store uses the other access mode".
            if resp.status_code in (400, 403) and i < len(order) - 1:
                logger.info("Blob upload with access=%s rejected (%s); trying %s",
                            access, resp.status_code, order[i + 1])
                continue
            break
        raise VercelBlobError(first_error)

    def _head(self, name: str) -> dict | None:
        resp = self._send("GET", self._api_url(), params={"url": self._full_path(name)},
                          headers=self._api_headers(), timeout=15, retries=1)
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise VercelBlobError(f"Vercel Blob head failed for '{name}' ({resp.status_code}).")
        return resp.json()

    def exists(self, name: str) -> bool:
        try:
            return self._head(name) is not None
        except OSError:
            # Report "doesn't exist" rather than crash get_available_name();
            # the upload itself refuses to overwrite and surfaces a clear error.
            return False

    def size(self, name: str) -> int:
        meta = self._head(name)
        if meta is None:
            raise FileNotFoundError(name)
        return int(meta.get("size") or 0)

    def url(self, name: str) -> str:
        # Only resolvable by browsers for public stores; private blobs are
        # delivered through views that check permissions (see module docstring).
        return self._blob_url(name, self._known_access() or "public")

    def delete(self, name: str) -> None:
        access = self._known_access()
        target = self._blob_url(name, access) if (access and self.store_id) else self._full_path(name)
        try:
            resp = self._send("POST", self._api_url("/delete"),
                              headers=self._api_headers(**{"content-type": "application/json"}),
                              json={"urls": [target]}, timeout=30)
            if resp.status_code >= 400:
                logger.warning("Vercel Blob delete of '%s' returned %s", name, resp.status_code)
        except OSError:
            # Best-effort (post_delete signal): the DB row is gone either way.
            logger.warning("Vercel Blob delete of '%s' failed", name, exc_info=True)

    def get_accessed_time(self, name):  # pragma: no cover - not tracked by Blob
        raise NotImplementedError("VercelBlobStorage doesn't record access times.")

    def get_created_time(self, name):  # pragma: no cover - not exposed here
        raise NotImplementedError("VercelBlobStorage doesn't expose creation times.")
