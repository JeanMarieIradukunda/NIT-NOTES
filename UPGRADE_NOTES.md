# Upgrade notes — Vercel deployment, Neon Postgres, Vercel Blob storage

This upgrade completes and fixes the platform's Vercel deployment setup —
earlier commits had started pointing settings.py at `DATABASE_URL` and
`.vercel.app` but left it non-functional (`dj-database-url` was imported in
settings.py but missing from `requirements.txt`, so the app would crash on
import in any environment that installed strictly from that file; there was
also no plan at all for where uploaded files would live, since Vercel's
filesystem doesn't persist them). No page, URL, permission rule or existing
feature changes — everything documented elsewhere in this file and in the
README still describes the app accurately. What changed here is entirely
about *where the database and files live in production*, plus one real bug
fix.

## What changed

1. **Fixed a real bug:** `dj-database-url` is now actually listed in
   `requirements.txt`. Without it, `pip install -r requirements.txt` followed
   by `python manage.py` anything would fail with `ModuleNotFoundError` the
   moment `DATABASE_URL` was ever set, since `nit_platform/settings.py`
   already imported it unconditionally.
2. **New: `core/storage.py`**, a small Django storage backend over the
   Vercel Blob REST API. Wired in as the storage for module notes
   (`core.models.private_storage`) and for everything else that gets
   uploaded or imported (`STORAGES["default"]` in settings.py) — but *only*
   when `BLOB_READ_WRITE_TOKEN` is set. With no token (local development, or
   a traditional server with a real disk), both fall back to plain local-disk
   storage exactly as before. This is the fix for the gap above: without it,
   Trainer uploads on Vercel would appear to succeed and then silently lose
   the file on the next request.
3. **Database settings refined for Neon specifically**: `conn_max_age`
   now defaults to `0` (configurable via `DB_CONN_MAX_AGE`) instead of a
   fixed `600`, since a serverless function has no long-running process to
   usefully hold a connection open across requests — Neon's own pooler
   (use the `-pooler` connection string) is what should be doing connection
   reuse. Also enabled `conn_health_checks`, so a connection that's gone
   stale between invocations is quietly replaced instead of failing the
   request.
4. **`ALLOWED_HOSTS` / `CSRF_TRUSTED_ORIGINS` generalised**: the previous
   version hard-coded one specific `nit-resources.vercel.app` hostname
   alongside the `.vercel.app` wildcard that already covers it (Django
   matches a leading-dot host against the domain and every subdomain) — the
   redundant literal is gone, so this now works unmodified under any Vercel
   project name.
5. **`vercel.json`** sets a 60-second function timeout (`maxDuration`),
   since PDF export and AI requests can run longer than Vercel Functions'
   short default. No `builds`/`routes` config is needed or included — recent
   Vercel Python runtime versions detect a Django project from `manage.py`
   and `WSGI_APPLICATION` automatically, including running `collectstatic`
   and serving `STATIC_URL` from the CDN.
6. **`.vercelignore`** keeps local-only data (`db.sqlite3`, `media/`,
   `private_media/`, `.env`, `venv/`) out of the deployment bundle.
7. **`.env.example`** documents the three Vercel-specific variables
   (`DATABASE_URL`, `DB_CONN_MAX_AGE`, `BLOB_READ_WRITE_TOKEN`) and notes
   that Vercel injects the first and third automatically once you connect a
   database and a Blob store — nothing to paste in by hand for a normal
   deployment.
8. **README** gained a full "Deploying to Vercel" walkthrough (connect
   Postgres, connect Blob, set the remaining env vars, deploy, then run
   migrations locally against the pulled production environment — Vercel
   has no long-lived process to run them from automatically) and documents
   the one real limitation this environment has: **WeasyPrint PDF export
   needs system libraries (Pango/Cairo) that Vercel's Python runtime doesn't
   provide.** The app already handled a missing WeasyPrint gracefully before
   this upgrade (a clear in-app message instead of a 500), so this isn't a
   new failure mode — it's now just documented as the expected behavior on
   Vercel specifically. **Print lesson** (the browser's own print dialog)
   is unaffected and needs no server dependency at all.

## Deploying this upgrade to an existing Vercel project

```bash
pip install -r requirements.txt      # picks up dj-database-url, requests
```

If you haven't already, connect a Blob store (Project → Storage → Blob →
Connect to Project) — otherwise this upgrade doesn't change anything for
you, since without `BLOB_READ_WRITE_TOKEN` the app behaves exactly as it did
before. If you deployed an earlier version of this project to Vercel
*without* a Blob store connected, any module notes, resources or lesson
uploads made in production since then were not actually persisted — they
appeared to save, then vanished on the next request. Re-upload them after
this upgrade ships; nothing about the database is affected.

---

# Upgrade notes — new design, empty module database, module notes

## What changed

1. **New interface.** The Solarized Dark theme is replaced by a light,
   professional design system (`static/css/platform.css`): navy/blue palette,
   Inter typography, consistent cards, tables, forms, badges and empty states,
   responsive down to phones. Every page was rebuilt on it, including the
   printable lesson PDF. Bootstrap, Bootstrap Icons and Inter are now bundled
   in `static/vendor/` (no CDN / Google Fonts), which also helps on slow
   connections. Text colours were checked against WCAG AA contrast.
2. **Module data removed.** All modules, units, lessons, resources, upload
   records and reading activity were deleted from the shipped database and the
   files behind them removed. User accounts, roles and the five levels were
   kept. The bundled `legacy_source/` archive was removed too. Repeatable with
   `python manage.py purge_module_data` (see README).
3. **Django admin restyled** (`/admin/`) to match: navy branded header with a
   shortcut to the Notes workspace, card-style panels, clean tables, blue
   buttons, styled filters/search/pagination/forms, a themed login page, and a
   matching dark mode (the admin's own light/dark/auto toggle still works). It is
   a stylesheet (`static/css/admin-theme.css`) plus one template override
   (`templates/admin/base_site.html`) — no admin logic was changed.
4. **Notebook look for published module notes.** HTML notes are shown as
   handwritten classroom notes on ruled paper: Patrick Hand (text), Caveat
   (headings) and Courier Prime (code), with marker-highlighted headings,
   sticky-note callouts and a red margin line. It is CSS only
   (`static/css/platform.css`, section "Notebook look", scoped to `#note-prose`)
   plus bundled font files in `static/vendor/handwriting/` (SIL Open Font
   Licence; self-hosted because the note reader only allows fonts from this
   site). Lessons and the rest of the interface keep the Inter look; to give
   lessons the notebook look too, add `#lesson-prose` to those selectors. PDF
   notes keep the fonts embedded in the PDF. Change `--rule` to adjust the
   ruled-line height.
5. **Module notes for Trainers** (`/notes/`): module code/name, title, PDF or
   HTML upload, publish / unpublish, edit, replace file, delete. Files are held
   in private storage and served through a permission-checked view.

## Deploying this upgrade to an existing site

```bash
pip install -r requirements.txt        # unchanged
python manage.py migrate               # adds ModuleNote; refreshes a help text
python manage.py collectstatic --noinput
```

`migrate` does **not** delete anything. Your live data is only removed if you
choose to run `python manage.py purge_module_data` — do that only if you really
want an empty module database, and back up `db.sqlite3` and `media/` first.

Optional new settings (see `.env.example`): `PRIVATE_MEDIA_ROOT`,
`MAX_NOTE_SIZE_MB`. Back up `PRIVATE_MEDIA_ROOT` with the database.

## Things to know

- **Trainer assignments were cleared** by the purge (they pointed at deleted
  modules). Trainers now register their own modules when adding notes, or an
  Administrator assigns modules from **Trainers → Modules**.
- `trainer_demo` was **suspended** in the database supplied; that was left as is.
- **Rotate your Groq API key.** The `.env` in the supplied archive contained
  what appears to be a live key. It has not been copied into this package.
- **Security hardening in the HTML sanitiser** (affects lessons too): embeds,
  objects, forms, `srcdoc`, `data:text/html` links, source CSS classes and
  `data-*` attributes are now removed. Removing source classes fixes uploaded
  Bootstrap pages whose collapsed sections were silently hidden.
- Fixed: `import_legacy` crashed on import (it referenced the removed Student
  role); error messages now use Bootstrap's `alert-danger` styling.
- Removed the unused `accounts/register.html` template (public registration
  no longer exists).

---

# Previous upgrade — roles, access control, Groq AI, content layout, Solarized Dark

This upgrade touches accounts/permissions, AI, content structure and the
visual theme. Nothing in the URL structure for reading lessons changed, so
existing bookmarks/links to lessons, modules and PDFs still work.

## 1. Deploying this upgrade

```bash
pip install -r requirements.txt   # groq replaces anthropic/openai
python manage.py migrate          # adds the "assignment" Resource kind
python manage.py bootstrap_roles  # ensures Trainer/Administrator groups exist
python manage.py collectstatic --noinput
```

Set `GROQ_API_KEY` (and optionally `GROQ_MODEL`, default
`llama-3.3-70b-versatile`) in the environment to enable the AI panel.

**Security note:** the `.env` file in this repo previously contained a live
Anthropic API key committed in plaintext. It has been removed as part of
this upgrade — rotate/revoke that key from the Anthropic console if it
hasn't been already, and make sure `.env` is git-ignored going forward.

## 2. Roles — what changed

- Public self-registration (`/accounts/register/`) is gone. There is no
  Student role/account anymore — students read the library without signing
  in, exactly as before, since the reading views never required login.
- Only an **Administrator** can create a **Trainer** account, from the new
  **Manage Trainers** screen (`/accounts/trainers/`, linked from the navbar
  for signed-in admins). Creating a Trainer there also assigns their
  modules in the same step.
- Existing `trainer_demo` / legacy accounts are unaffected. A pre-existing
  `student_demo` account (if present) still exists in the database but has
  no special meaning anymore — it's just an ordinary Django user with no
  role.

## 3. Trainer access — now strictly enforced

Previously, a Trainer with no modules explicitly assigned could manage
*any* module — a real gap. That's fixed: a Trainer now manages **only**
the modules an Administrator has assigned them via `Profile.trainer_modules`.
No assignment means no access, not open access. This is enforced twice:

- In the UI — the "Upload new lesson notes" button only appears on a
  Trainer's own modules.
- Server-side, in `core/views.py:upload_lesson` — visiting another
  Trainer's upload URL directly now returns a 403 page, it doesn't rely on
  the button being hidden.

Reassign a Trainer's modules at any time from **Manage Trainers → Modules**.

## 4. AI — Groq only

`ai_tools/services.py` now calls Groq exclusively; the Anthropic/OpenAI code
paths are gone. Behavior with no key configured is unchanged (the panel
explains AI is off rather than erroring).

## 5. Learner Notes vs. Assessments & Assignments

Every module page now has three clearly separated, distinctly styled
sections instead of two:

- **Lesson notes** — the existing unit/lesson listing, unchanged in
  behavior, now under its own heading.
- **Assessments & assignments** — `Resource` gained a new `"assignment"`
  kind alongside the existing `"assessment"` kind; both are grouped here
  with a distinct accent color and a left-border treatment so they read as
  graded/practice work, not reading material.
- **Files & downloads** — everything else (PDFs, code, datasets, images).

## 6. Solarized Dark

`static/css/platform.css` was rewritten around the standard Solarized Dark
palette (base03/base02/base01/base00 for surfaces and text, cyan/blue for
the accent pair, yellow for the "assessments" accent, red for alerts). The
IBM Plex Sans/Serif/Mono font stack is unchanged, per the brief. The site
now always renders in this single dark theme (`data-bs-theme="dark"` in
`base.html`); the previous light theme is gone. The printable PDF export
(WeasyPrint) is untouched — it stays light/print-friendly regardless of the
site theme, since print documents shouldn't be dark.
