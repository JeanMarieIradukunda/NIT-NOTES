"""
Django settings for the NIT Learning Resources platform.

Everything environment-specific (secret key, debug, database, AI provider key)
is read from the environment so the same codebase runs in development and in
production without edits. Sensible local defaults are provided so the project
runs out of the box with `python manage.py runserver`.
"""

import os
from pathlib import Path

from django.contrib.messages import constants as message_constants
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent

# --------------------------------------------------------------------------- #
# Core
# --------------------------------------------------------------------------- #

SECRET_KEY = os.environ.get(
    "DJANGO_SECRET_KEY",
    "dev-only-insecure-key-change-me-before-deploying",
)
DEBUG = os.environ.get("DJANGO_DEBUG", "true").lower() == "true"


# A leading dot in ALLOWED_HOSTS matches the domain AND every subdomain, so
# ".vercel.app" alone covers this project's production domain, every preview
# deployment, and any Vercel project name/rename — no need to hard-code one
# specific *.vercel.app hostname. It's kept in the list even when
# DJANGO_ALLOWED_HOSTS is set in the Vercel dashboard, so overriding the env
# var can't accidentally 400 every preview deployment with DisallowedHost.
# A custom domain (e.g. resources.yourschool.ac.rw) is NOT a *.vercel.app
# subdomain, so add it to DJANGO_ALLOWED_HOSTS if you attach one.
ALLOWED_HOSTS = list({
    *(h.strip() for h in os.environ.get("DJANGO_ALLOWED_HOSTS", "").split(",") if h.strip()),
    ".vercel.app",
    "",
})

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",

    "core.apps.CoreConfig",
    "accounts.apps.AccountsConfig",
    "ai_tools.apps.AiToolsConfig",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "core.middleware.RoleContextMiddleware",
]

ROOT_URLCONF = "nit_platform.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "core.context_processors.brand",
                "core.context_processors.admin_dashboard",
            ],
        },
    },
]

WSGI_APPLICATION = "nit_platform.wsgi.application"


# --------------------------------------------------------------------------- #
# Database — Neon Postgres in production, SQLite for local development
# --------------------------------------------------------------------------- #
#
# SQLite doesn't work on Vercel: the deployed function's filesystem is
# read-only (and ephemeral per-instance even where it isn't), and db.sqlite3
# is gitignored anyway, so it doesn't exist in the deployed bundle at all —
# every query fails with "unable to open database file". Use a real Postgres
# database via DATABASE_URL in production, and keep SQLite only for local
# development.
#
# Neon is the reference target here: connect it from the Vercel dashboard
# (Storage -> Postgres, or the Neon integration) and Vercel injects
# DATABASE_URL automatically — nothing to paste in by hand. Any other
# Postgres provider works the same way, since this is just a standard
# DATABASE_URL.
#
# Neon note: use the *pooled* connection string it gives you (the one with
# "-pooler" in the hostname), not the direct one. Vercel Functions are
# short-lived and can run many of them at once, and Neon's pooler is built
# for exactly that; the direct connection string can exhaust Neon's own
# connection limit under real traffic.
import dj_database_url

DATABASE_URL = os.environ.get("DATABASE_URL")

if DATABASE_URL:
    DATABASES = {
        "default": dj_database_url.parse(
            DATABASE_URL,
            # Serverless functions are short-lived processes; there's no
            # long-running worker to usefully keep a connection open across
            # requests the way conn_max_age assumes. 0 (open a fresh, pooled
            # connection per request — cheap, since Neon's pooler is doing
            # the real connection reuse) is the safe default. Override with
            # DB_CONN_MAX_AGE if you're running this on a traditional,
            # always-on server instead, where a larger value (e.g. 600) is
            # the usual choice.
            conn_max_age=int(os.environ.get("DB_CONN_MAX_AGE") or "0"),
            conn_health_checks=True,
            ssl_require=True,
        )
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# --------------------------------------------------------------------------- #
# I18N
# --------------------------------------------------------------------------- #

LANGUAGE_CODE = "en-us"
TIME_ZONE = (os.environ.get("DJANGO_TIME_ZONE") or "UTC").strip()
USE_I18N = True
USE_TZ = True

# --------------------------------------------------------------------------- #
# Static & media
# --------------------------------------------------------------------------- #

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"

# Vercel Blob storage (see core/storage.py) for uploaded/imported files —
# Resource uploads, past papers, imported images, lesson source HTML, and
# (via core.models.private_storage) module notes — whenever a Blob store is
# connected. Connect one from the Vercel dashboard (Storage -> Blob ->
# Connect to Project) and BLOB_READ_WRITE_TOKEN is injected automatically.
#
# This matters for the same reason SQLite doesn't work above: Vercel's
# filesystem doesn't persist uploads between requests, so without this,
# "Add module notes" and "Upload lesson" would appear to work and then
# silently lose the file. With no token (local development, or a
# traditional always-on server with a real disk), this falls back to plain
# local-disk storage exactly as before — nothing about that setup changes.
BLOB_READ_WRITE_TOKEN = os.environ.get("BLOB_READ_WRITE_TOKEN", "")

# Vercel sets its own VERCEL=1 env var on every deployment. If we're running
# there with no Blob token, both STORAGES["default"] below and
# core.models.private_storage() would silently fall back to
# FileSystemStorage — which then blows up the first time anything is
# uploaded, with a confusing "Read-only file system" OSError deep in
# Django's save() call. Failing here instead, at startup, with a clear
# message pointing at the actual missing step, is much easier to diagnose.
if os.environ.get("VERCEL") and not BLOB_READ_WRITE_TOKEN:
    raise ImproperlyConfigured(
        "Running on Vercel but BLOB_READ_WRITE_TOKEN is not set. Connect a "
        "Blob store to this project in the Vercel dashboard (Storage -> "
        "Blob -> Connect to Project), which injects the token "
        "automatically, then redeploy. Without it, file uploads (resources, "
        "lesson uploads, module notes) will fail."
    )

STORAGES = {
    "default": (
        {"BACKEND": "core.storage.VercelBlobStorage", "OPTIONS": {"prefix": "media"}}
        if BLOB_READ_WRITE_TOKEN
        else {"BACKEND": "django.core.files.storage.FileSystemStorage"}
    ),
    # WhiteNoise here is for local development (`python manage.py runserver`)
    # and traditional/self-hosted deployments. On Vercel itself this setting
    # is beside the point: Vercel detects STATIC_ROOT, runs collectstatic
    # during the build automatically, and serves everything under
    # STATIC_URL straight from its CDN in front of the function — faster
    # than WhiteNoise and with no extra configuration. See the README's
    # "Deploying to Vercel" section.
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}

MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

# Uploaded notes are HTML fragments — small, trusted-author files. Cap size so
# an accidental large upload (or a video renamed to .html) is rejected cleanly.
#
# NOTE: `os.environ.get(KEY, default)` only falls back to `default` when KEY is
# completely unset. If a hosting platform (e.g. Vercel) defines the variable
# but leaves its value blank, `.get()` returns "" instead of the default, and
# `int("")` raises ValueError at import time, crashing the whole app. Using
# `os.environ.get(KEY) or default` falls back on both "unset" AND "empty".
MAX_UPLOAD_SIZE_MB = int(os.environ.get("MAX_UPLOAD_SIZE_MB") or "8")

# Module notes (PDF / HTML uploaded by Trainers) live OUTSIDE MEDIA_ROOT, in a
# private folder that is never served directly. Files are only ever delivered
# through the permission-checked `core:note_file` view, so unpublishing a note
# really does make its file unreachable. Back this folder up with the database.
PRIVATE_MEDIA_ROOT = Path(os.environ.get("PRIVATE_MEDIA_ROOT") or (BASE_DIR / "private_media"))

# Size cap for one module-notes file (PDF or HTML), in megabytes.
MAX_NOTE_SIZE_MB = int(os.environ.get("MAX_NOTE_SIZE_MB") or "25")
MAX_ACTIVITY_SIZE_MB = int(os.environ.get("MAX_ACTIVITY_SIZE_MB") or "25")

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --------------------------------------------------------------------------- #
# Auth / roles
# --------------------------------------------------------------------------- #

LOGIN_URL = "accounts:login"
LOGIN_REDIRECT_URL = "core:dashboard"
LOGOUT_REDIRECT_URL = "core:dashboard"

# Two roles: Administrator and Trainer. Students browse the library without
# an account, so there is no Student role. Groups are created automatically
# by the `bootstrap_roles` management command (also run inside `import_legacy`).
ROLE_TRAINER = "Trainer"
ROLE_ADMIN = "Administrator"

# --------------------------------------------------------------------------- #
# AI features — Groq is the platform's only AI provider. The platform runs
# fully without a key; the AI panel explains how to switch it on.
# See ai_tools/services.py.
# --------------------------------------------------------------------------- #

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")

# --------------------------------------------------------------------------- #
# Misc
# --------------------------------------------------------------------------- #

MESSAGE_STORAGE = "django.contrib.messages.storage.session.SessionStorage"
# Bootstrap calls the error alert `alert-danger`; Django's default tag is "error",
# which would render as an unstyled `alert-error`.
MESSAGE_TAGS = {message_constants.ERROR: "danger"}
SILENCED_SYSTEM_CHECKS = ["staticfiles.W004"]

CSRF_TRUSTED_ORIGINS = list({
    *(h.strip() for h in os.environ.get("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",") if h.strip()),
    "https://*.vercel.app",
})