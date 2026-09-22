# NIT Learning Resources — Django edition

Module notes, lessons and past papers for National IT levels 3 to 5 at Padri
Vjeko Centre TSS: a Django application with real accounts and roles, a
database-backed content model, a **module notes** workspace where Trainers
upload, publish and manage PDF/HTML notes, server-generated A4 PDFs, and an
optional AI study assistant.

Built with **Django**, **Bootstrap 5** and **Inter**, with **WeasyPrint** for
PDF export. Bootstrap, Bootstrap Icons and Inter are bundled in
`static/vendor/`, so the interface renders fully offline and never depends on a
CDN or Google Fonts.

---

## Quick start

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

python manage.py migrate
python manage.py seed_demo          # creates admin / trainer_demo (skip if you already have accounts)
python manage.py collectstatic --noinput

python manage.py runserver
```

Open `http://127.0.0.1:8000/`. The library needs no account: anyone can browse,
read and download published notes. **The module database starts empty** — sign
in as a Trainer and use **Add module notes** to create the first modules and
notes. Demo staff accounts created by `seed_demo` (password `ChangeMe!2026` —
**change it before deploying anywhere public**):

| Username | Role |
|---|---|
| `admin` | Administrator (superuser, full `/admin/`, manages Trainers and every note) |
| `trainer_demo` | Trainer (adds and manages their own module notes) |

---

## Architecture

```
nit_platform/       Django project settings, root urls
core/                the platform itself
  models.py            Trade → Module → Unit → Lesson, Resource, StudentActivity,
                       LessonUpload, ModuleNote
  note_views.py        module notes: Trainer workspace, reader, secure file delivery
  forms.py             upload validation (extension + content sniffing, size, HTML parsing)
  html_processing.py   sanitises uploaded HTML into a safe fragment (lessons and notes)
  legacy_import.py     reference-repair engine used by import_legacy
  storage.py           Vercel Blob storage backend, used when BLOB_READ_WRITE_TOKEN is set
  views.py             dashboard, browse, module, lesson reader, PDF export, search, lesson upload
  tests.py             automated tests (python manage.py test)
  management/commands/ import_legacy, purge_module_data
accounts/            Trainer / Administrator roles (Django Groups); students never sign in
  roles.py             permission helpers (is_trainer, is_admin, can_manage, can_manage_note, ...)
  views.py             admin-only Trainer management + own profile
  management/commands/ bootstrap_roles, seed_demo
ai_tools/            AI study assistant (Groq)
templates/           Bootstrap 5 templates; templates/admin/ brands the Django admin
static/css/          platform.css — the design system (tokens, components, reader typography)
static/vendor/       self-hosted Bootstrap, Bootstrap Icons, Inter and the notebook
                     handwriting fonts (handwriting/: Patrick Hand, Caveat, Courier Prime)
private_media/       local dev / traditional server only: uploaded module-notes files
                     (never publicly routable). On Vercel this is Vercel Blob storage
                     instead — see "Deploying to Vercel" and core/storage.py.
```

### Content model

```
Trade  (Level 3 / 4 / 5 NIT, or the Past Papers archive)
  └─ Module  (e.g. GENCP302 — C Programming Fundamentals)
       └─ Unit  (a learning outcome, or a topic section for modules that
                  aren't organised by outcome — Python's 15 topic folders,
                  for example)
            └─ Lesson  (the readable page — one row per HTML file)
       └─ Resource  (past papers, marking guides, images, videos, and the
                      original interactive assessment pages, preserved as-is)
```

Nothing about a trade or module is hard-coded in a template — everything
renders from these tables. `core/legacy_taxonomy.json` supplies human-readable
names during import; without an entry the importer falls back to the folder
name, so a brand new folder still imports cleanly.

---

## Module notes (Trainers)

Signed-in Trainers and Administrators get **My notes** (Administrators: **All
notes**) in the navigation, at `/notes/`.

**Adding notes** (`/notes/new/`)

1. **Module** — select one of your modules, or choose *Enter new module* and give
   its code, name and level. A new module is created and assigned to you.
2. **Title** — what students see.
3. **File** — a `.pdf` or `.html`/`.htm` file (up to `MAX_NOTE_SIZE_MB`, default 25).
4. **Publish now** or **Save as draft**. Pressing Enter in the form always saves a draft.

**Managing notes** (`/notes/`) — filter by status, module or text. For each note:
**Edit** (change title or module, or replace the file — a new file fully replaces
the old one and may change type), **Publish / Unpublish**, and **Delete**
(with confirmation; the stored file is removed too).

**How HTML notes look** — as handwritten notes on ruled paper (the "Notebook
look" section of `static/css/platform.css`, scoped to `#note-prose`). The
author's own fonts and styles are stripped on upload, so this stylesheet alone
decides the look.

**What students see** — published notes appear on the module page, the
dashboard ("Latest module notes") and in search (title, module and, for HTML
notes, the full text). PDFs open in a built-in viewer with a download button;
HTML notes are shown as a readable page with a contents rail, and can be
downloaded. Drafts are visible only to their author and Administrators.

**Who can do what**

| | Anyone | Trainer | Administrator |
|---|---|---|---|
| Read / download published notes | yes | yes | yes |
| Add notes | | yes | yes |
| Edit, publish, unpublish, delete | | own notes only | any note |
| See drafts | | own drafts | all |

A Trainer may file notes under modules assigned to them by an Administrator, or
under a module they register themselves. Entering a code that already exists and
isn't theirs is refused with an explanation — an Administrator can assign it to
them from **Trainers → Modules**. Deleting a Trainer's account keeps their notes
(they become Administrator-managed).

**Security model**

- Note files live in private storage, *outside* the public media folder,
  under random file names — `PRIVATE_MEDIA_ROOT` (default `private_media/`)
  locally or on a traditional server, Vercel Blob storage (prefixed
  `private_media/` there too) when deployed to Vercel. Either way they are
  only ever delivered by `/notes/<id>/file/`, which re-checks
  published/author/admin on every request — so unpublishing genuinely hides
  a file, and the app never prints a note's storage URL into a template for
  someone to bookmark around that check.
- Uploads are validated by content, not just extension: a `.pdf` must start with
  `%PDF-`; an HTML file is parsed; a PDF renamed `.html` (or the reverse) is refused.
- HTML notes are sanitised on upload (scripts, styles, embeds, forms, event
  handlers, `javascript:`/`data:text/html` URLs, source CSS classes and `data-*`
  attributes are removed) and the reader page is served with a strict
  Content-Security-Policy as a second layer. The original HTML file is offered
  only as a download, never rendered as a live page.
- Images in an HTML note must be embedded in the file (`data:` URIs); images
  linked from other files can't be loaded and become a labelled placeholder.

### Structured lessons (unchanged)

Module pages still offer **Upload lesson** to people who manage that module. It
turns one HTML file into a `Lesson` (learning-outcome grouping, reading
progress, bookmarks, AI assistant, PDF export). Use module notes for simple
PDF/HTML notes and lessons for structured reading.

**Roles**: there is no public registration. An Administrator creates every
Trainer account from **Trainers** (`/accounts/trainers/`).

---

## Starting from an empty module database

`python manage.py purge_module_data` removes every module, unit, lesson,
resource, module note, upload record and reading-activity row, plus the files
behind them, while keeping **user accounts, roles and levels**. It lists what it
will delete and asks you to type `delete` (or pass `--yes`). Use `--dry-run` to
preview and `--include-levels` to remove levels too. It is irreversible — back
up first.

The data that shipped with the previous version was removed this way. The
original static archive is no longer bundled; `import_legacy --source <folder>`
can re-import a copy of it if you ever need to.

---

## PDF export

**Download PDF** on every lesson calls WeasyPrint server-side. This is CSS
paged media, not a screenshot: the lesson's own HTML is laid out onto real A4
page boxes, so the text stays selectable, searchable and copyable.

Each PDF has:

- A4, 26/18/20/18 mm margins
- a running header (institution + module code) and footer
  (platform + generation date + `Page N of M`) generated by CSS `@page`
  rules and `counter(page)` / `counter(pages)` — no JavaScript involved
- the trade › module › outcome trail and reading stats on the first page
- tables, code blocks, callouts and images kept intact, with page-break
  avoidance on headings, table rows, code blocks and callouts

**Print lesson** uses the browser's own print dialog against the same page,
with all platform chrome hidden by `@media print` rules — a fallback that
needs no server dependency at all.

### If PDF export doesn't work after `pip install`

WeasyPrint needs Pango and Cairo as system libraries, not just the Python
package:

```bash
# Debian / Ubuntu
sudo apt-get install libpango-1.0-0 libpangocairo-1.0-0 libcairo2 \
                     libgdk-pixbuf2.0-0 libffi-dev

# macOS
brew install pango
```

Without them, the **Download PDF** button shows a clear in-app message
explaining what's missing rather than a 500 error.

---

## AI study assistant

Two features live on every lesson page (lessons only, not module notes), both grounded only in that lesson's
own text (`ai_tools/services.py`). **Groq is the platform's only AI
provider** — there is no other provider to configure:

- **Summarise this lesson** — a short, exam-focused revision summary
- **Ask about this lesson** — answers a student's question using the lesson's
  notes as context

The platform runs completely normally with **no key configured** — the panel
explains this plainly instead of erroring. To switch it on, get a key from
[console.groq.com/keys](https://console.groq.com/keys) and set it as an
environment variable — locally:

```bash
export GROQ_API_KEY=gsk_...
# optional, defaults to llama-3.3-70b-versatile — see
# https://console.groq.com/docs/models for the current model list:
export GROQ_MODEL=llama-3.3-70b-versatile
```

or on Vercel, as a Project → Settings → Environment Variable of the same
name (see `.env.example` and "Deploying to Vercel" below) — no code change
either way.

---

## Roles and permissions

Two Django Groups, created automatically by `bootstrap_roles` (also run
inside `import_legacy`):

| Role | Can do |
|---|---|
| **Trainer** | Add module notes and manage **their own** notes; upload lessons for their assigned modules |
| **Administrator** | Everything, plus full `/admin/` and the Manage Trainers screen — create/suspend Trainer accounts, assign modules, manage every trade, module, lesson and resource |

Students have no account at all: every lesson, resource and assessment is
readable and downloadable without signing in.

`accounts/roles.py` centralises every permission check
(`is_trainer`, `is_admin`, `can_upload`, `can_manage`, `can_add_notes`, `can_manage_note`) rather than scattering
group lookups across views. Django superusers are always treated as
Administrators regardless of group membership.

`Profile.trainer_modules` is how a Trainer's access is scoped. This is
**strict by default**: a Trainer with no modules assigned can manage
nothing. An Administrator assigns modules when creating the Trainer
(`/accounts/trainers/new/`) or later from `/accounts/trainers/` → **Modules**.
This is enforced both in the UI (the upload button only appears on a
Trainer's own modules) and server-side in the view itself, so it can't be
bypassed by guessing a URL.

---

## Deploying

### Deploying to Vercel (recommended)

The codebase is set up for this out of the box: **Neon Postgres** for the
database and **Vercel Blob** for uploaded files, alongside the existing
**Groq** AI assistant. Nothing below requires editing code — it's all
environment variables and two clicks in the Vercel dashboard.

1. **Push this repo to GitHub/GitLab/Bitbucket** and [import it as a Vercel
   project](https://vercel.com/new). Vercel detects Django automatically
   from `manage.py` and `WSGI_APPLICATION` — no `vercel.json` builds/routes
   config needed (the `vercel.json` in this repo only sets a longer function
   timeout for PDF export and AI requests).
2. **Connect a Postgres database** — Project → Storage → connect **Neon**
   (or Vercel's own Postgres, which is Neon-backed). This injects
   `DATABASE_URL` automatically. Use the connection string with `-pooler` in
   the hostname if you ever set it by hand; see `.env.example`.
3. **Connect a Blob store** — Project → Storage → **Blob** → Connect to
   Project. This injects `BLOB_READ_WRITE_TOKEN` automatically. **This step
   matters**: without it, every module note, resource and lesson upload will
   appear to save successfully and then silently vanish, because Vercel's
   filesystem doesn't persist writes between requests (see `core/storage.py`
   for the full explanation). With it, uploads go to Vercel Blob instead and
   behave exactly as documented above.
4. **Set the remaining environment variables** (Project → Settings →
   Environment Variables) — see `.env.example` for the full list:
   - `DJANGO_SECRET_KEY` — a long random string
   - `DJANGO_DEBUG=false`
   - `DJANGO_ALLOWED_HOSTS` — only needed if you attach a custom domain
     (`*.vercel.app` is already trusted)
   - `GROQ_API_KEY` (+ optionally `GROQ_MODEL`) — to turn on the AI
     assistant; leave empty to keep it off
5. **Deploy.** Vercel runs `collectstatic` for you during the build and
   serves everything under `/static/` from its CDN — no extra static-file
   configuration needed.
6. **Run migrations and seed the first accounts.** Migrations don't run
   automatically on Vercel (there's no long-lived server to run them from,
   and running them from inside a request would be fragile). Pull the
   project's environment locally and run them from there:
   ```bash
   vercel env pull .env.local
   set -a; . ./.env.local; set +a
   python manage.py migrate
   python manage.py bootstrap_roles
   python manage.py seed_demo   # optional — creates admin / trainer_demo
   ```
   Do this again after any migration-adding change. `import_legacy` and
   `purge_module_data` are run the same way — they're maintenance commands,
   not part of the live app, and both write straight to the database/storage
   your `.env.local` points at, so double-check `DATABASE_URL` before running
   `purge_module_data` against production.

**Known limitation on Vercel: PDF export.** **Download PDF** uses
WeasyPrint, which needs the Pango/Cairo system libraries — Vercel's Python
runtime doesn't include them, and there's currently no supported way to add
system packages to it. The app already degrades gracefully for this (see
"PDF export" below): the button shows a clear in-app explanation instead of
a 500 error, and **Print lesson** (the browser's own print dialog, no
server dependency at all) keeps working normally. If you need server-side
PDF export to work on Vercel specifically, run it as a small separate
service instead (a container platform with WeasyPrint's system libraries
installed) and point the button at it — that's a code change this upgrade
doesn't make, since it's a different piece of infrastructure, not a
settings change.

### Deploying to a traditional server

```bash
pip install -r requirements.txt
python manage.py migrate
python manage.py collectstatic --noinput
gunicorn nit_platform.wsgi:application --bind 0.0.0.0:8000
```

Set these environment variables in production (see `.env.example`):

- `DJANGO_SECRET_KEY` — a long random string, not the development default
- `DJANGO_DEBUG=false`
- `DJANGO_ALLOWED_HOSTS` — your real domain(s)
- `DATABASE_URL` — point it at Neon or any other Postgres server; leaving it
  unset falls back to SQLite, which is fine for a single small local
  deployment but not recommended anywhere with real traffic or backups to
  manage centrally

Leave `BLOB_READ_WRITE_TOKEN` unset here — a traditional server has a real,
persistent disk, so both storages fall back to it automatically:

- Set `PRIVATE_MEDIA_ROOT` to a persistent folder if you don't want it under
  the project directory. It holds every uploaded module-notes file, so
  **back it up together with the database** and keep it out of any public
  web-server folder.
- Uploaded and imported media (`media/`) should sit behind your web server or
  a persistent volume; it is not safe to lose on redeploy, since it holds
  every uploaded lesson's source file and every imported image/PDF/video.

Static files are served by WhiteNoise out of the box — no separate nginx
config needed for a small deployment.

---

## Testing

```bash
python manage.py test
```

The suite (`core/tests.py`) covers adding, editing, replacing, publishing,
unpublishing and deleting notes; upload validation (disguised and oversize
files); who can see or change what (drafts, other Trainers' notes, anonymous
visitors); file-delivery headers; the purge command; the HTML sanitiser; and
that every page renders on an empty database. Tests run against temporary
folders and never touch your real `media/` or `private_media/`.

---

## History

The project began as a static HTML site with no backend (`RESOURCES.rar`), was
rebuilt as this Django application, and was then upgraded to the current
design system and module-notes workflow — see `UPGRADE_NOTES.md`.
