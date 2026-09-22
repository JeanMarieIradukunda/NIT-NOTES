"""
core.models
============

The schema mirrors the curriculum's own shape:

    Trade -> Module -> Unit (a learning outcome, or a topic folder for
             modules that aren't organised by outcome) -> Lesson

Resource covers everything that isn't a lesson page: PDFs, past papers,
interactive assessment pages, images, code samples, video.

StudentActivity is one row per (user, lesson) and carries bookmarking,
completion and reading progress — backed by the database, since the platform
has real accounts.

LessonUpload is the audit trail behind the "upload new notes" feature: it
keeps the original HTML file next to the Lesson it produced.

ModuleNote is the Trainer-facing "module notes" feature: a titled PDF or HTML
file filed under a module, which the Trainer can publish, unpublish, replace
and delete. Its file is held in private storage and only ever served through a
permission-checked view (see core/note_views.py).
"""

import os
import re
import uuid

from django.conf import settings
from django.core.files.storage import FileSystemStorage
from django.core.validators import FileExtensionValidator
from django.db import models
from django.db.models.signals import post_delete
from django.dispatch import receiver
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify


def upload_resource_path(instance, filename):
    return f"resources/{instance.module.trade.key}/{instance.module.key}/{filename}"


def upload_lesson_source_path(instance, filename):
    trade = instance.module.trade.key if instance.module_id else "misc"
    module = instance.module.key if instance.module_id else "misc"
    return f"lesson_uploads/{trade}/{module}/{filename}"


def private_storage():
    """
    Storage for Trainer-uploaded module notes — outside MEDIA_ROOT, never
    publicly routable, only ever reached through the permission-checked
    `core:note_file` view (see core/note_views.py).

    On Vercel (or anywhere `BLOB_READ_WRITE_TOKEN` is set) this is Vercel
    Blob storage, since the local filesystem doesn't persist between
    requests there; otherwise it's a local folder, exactly as before. See
    core/storage.py for the full explanation.
    """
    if getattr(settings, "BLOB_READ_WRITE_TOKEN", ""):
        from core.storage import VercelBlobStorage
        return VercelBlobStorage(prefix="private_media")
    return FileSystemStorage(location=settings.PRIVATE_MEDIA_ROOT)


def upload_note_path(instance, filename):
    """Random file name: no collisions, and nothing guessable from the URL/disk."""
    ext = os.path.splitext(filename)[1].lower()
    return (f"module_notes/{instance.module.trade.key}/{instance.module.key}/"
            f"{uuid.uuid4().hex}{ext}")


def human_size(b):
    if b < 1024:
        return f"{b} B"
    if b < 1024 * 1024:
        return f"{b // 1024} KB"
    return f"{b / 1048576:.1f} MB"


class Trade(models.Model):
    """A level / programme, e.g. "Level 4 — National IT", or the past-papers archive."""

    KIND_TRADE = "trade"
    KIND_ARCHIVE = "archive"
    KIND_CHOICES = [(KIND_TRADE, "Trade"), (KIND_ARCHIVE, "Archive")]

    key = models.SlugField(max_length=40, unique=True,
                            help_text="Short machine key, e.g. L4, L5NIT.")
    name = models.CharField(max_length=160)
    short_name = models.CharField(max_length=60, blank=True)
    summary = models.CharField(max_length=280, blank=True)
    kind = models.CharField(max_length=10, choices=KIND_CHOICES, default=KIND_TRADE)
    order = models.PositiveSmallIntegerField(default=99)

    class Meta:
        ordering = ["order", "name"]

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("core:browse") + f"?trade={self.key}"

    @property
    def lesson_count(self):
        return Lesson.objects.filter(unit__module__trade=self).count()

    @property
    def note_count(self):
        return ModuleNote.objects.filter(module__trade=self, is_published=True).count()


class Module(models.Model):
    """A curriculum module within a trade, e.g. GENCP302 — C Programming Fundamentals."""

    trade = models.ForeignKey(Trade, related_name="modules", on_delete=models.CASCADE)
    key = models.SlugField(max_length=60, help_text="Folder-style key, e.g. GENCP302.")
    code = models.CharField(max_length=30, help_text="Displayed module code.")
    name = models.CharField(max_length=200)
    order = models.PositiveSmallIntegerField(default=99)

    class Meta:
        ordering = ["order", "code"]
        unique_together = [("trade", "key")]

    def __str__(self):
        return f"{self.code} — {self.name}"

    def get_absolute_url(self):
        return reverse("core:module_detail", args=[self.trade.key, self.key])

    @property
    def lesson_count(self):
        return Lesson.objects.filter(unit__module=self).count()

    @property
    def published_note_count(self):
        return self.notes.filter(is_published=True).count()

    @property
    def total_minutes(self):
        return sum(self.lessons().values_list("minutes", flat=True))

    def lessons(self):
        return Lesson.objects.filter(unit__module=self)


class Unit(models.Model):
    """A learning outcome, or (for topic-organised modules) a subject section."""

    KIND_OUTCOME = "outcome"
    KIND_TOPIC = "topic"
    KIND_CHOICES = [(KIND_OUTCOME, "Learning outcome"), (KIND_TOPIC, "Topic")]

    module = models.ForeignKey(Module, related_name="units", on_delete=models.CASCADE)
    code = models.CharField(max_length=40, help_text='e.g. "LO2" or a topic slug.')
    title = models.CharField(max_length=220)
    kind = models.CharField(max_length=10, choices=KIND_CHOICES, default=KIND_OUTCOME)
    order = models.PositiveSmallIntegerField(default=99)

    class Meta:
        ordering = ["order", "code"]
        unique_together = [("module", "code")]

    def __str__(self):
        return f"{self.module.code} · {self.title}"

    @property
    def is_general(self):
        return self.code == "LO0"


class Lesson(models.Model):
    """One readable lesson. `content_html` is a sanitised fragment, ready to render."""

    unit = models.ForeignKey(Unit, related_name="lessons", on_delete=models.CASCADE)
    title = models.CharField(max_length=220)
    slug = models.SlugField(max_length=240, blank=True, unique=True)

    content_html = models.TextField(
        help_text="Sanitised HTML fragment — the lesson body, no page chrome.")
    source_file = models.CharField(
        max_length=400, blank=True,
        help_text="Original file path this lesson came from, if imported.")
    original_url = models.CharField(max_length=400, blank=True)

    words = models.PositiveIntegerField(default=0)
    minutes = models.PositiveSmallIntegerField(default=1)
    tables = models.PositiveSmallIntegerField(default=0)
    images = models.PositiveSmallIntegerField(default=0)
    code_blocks = models.PositiveSmallIntegerField(default=0)
    headings_json = models.JSONField(default=list, blank=True)
    search_text = models.TextField(blank=True, help_text="Plain text, used for search.")

    order = models.PositiveSmallIntegerField(default=99)
    is_published = models.BooleanField(default=True)

    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="uploaded_lessons")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["order", "title"]

    def __str__(self):
        return self.title

    def save(self, *args, **kwargs):
        if not self.slug:
            base = slugify(self.title)[:200] or "lesson"
            candidate, n = base, 2
            while Lesson.objects.exclude(pk=self.pk).filter(slug=candidate).exists():
                candidate = f"{base}-{n}"
                n += 1
            self.slug = candidate
        super().save(*args, **kwargs)

    def get_absolute_url(self):
        return reverse("core:lesson_detail", args=[self.slug])

    def get_pdf_url(self):
        return reverse("core:lesson_pdf", args=[self.slug])

    @property
    def module(self):
        return self.unit.module

    @property
    def trade(self):
        return self.unit.module.trade

    @property
    def breadcrumb(self):
        parts = [self.trade.name, f"{self.module.code} {self.module.name}"]
        if self.unit.kind == Unit.KIND_OUTCOME and not self.unit.is_general:
            parts.append(self.unit.title)
        elif self.unit.kind == Unit.KIND_TOPIC:
            parts.append(self.unit.title)
        return parts

    def neighbours(self):
        siblings = list(self.unit.module.lessons().order_by("order", "title"))
        idx = next((i for i, l in enumerate(siblings) if l.pk == self.pk), None)
        if idx is None:
            return None, None
        prev = siblings[idx - 1] if idx > 0 else None
        nxt = siblings[idx + 1] if idx < len(siblings) - 1 else None
        return prev, nxt

    def pdf_filename(self):
        code = re.sub(r"[\\/]", "-", self.module.code)
        parts = [code]
        if self.unit.kind == Unit.KIND_OUTCOME and not self.unit.is_general:
            parts.append(self.unit.code)
        parts.append(self.title)
        name = "_".join(parts)
        name = re.sub(r"[^\w\s-]", "", name).strip()
        name = re.sub(r"\s+", "_", name)[:110]
        return (name or "Lesson") + ".pdf"


class Resource(models.Model):
    """A downloadable file: past paper, marking guide, dataset, image, code sample…"""

    KIND_CHOICES = [
        ("pdf", "PDF"), ("doc", "Word document"), ("text", "Text"),
        ("code", "Code / data"), ("image", "Image"), ("video", "Video"),
        ("assessment", "Interactive assessment"), ("assignment", "Assignment"),
    ]

    # Kinds that belong under "Assessments & Assignments" rather than under
    # plain "Files & Downloads" — keeps the student-facing separation clear
    # without needing a second model.
    ASSESSMENT_KINDS = ("assessment", "assignment")

    module = models.ForeignKey(Module, related_name="resources", on_delete=models.CASCADE)
    name = models.CharField(max_length=220)
    kind = models.CharField(max_length=12, choices=KIND_CHOICES)
    file = models.FileField(upload_to=upload_resource_path, max_length=400,
                             blank=True, null=True)
    external_path = models.CharField(
        max_length=400, blank=True,
        help_text="Used for imported legacy files served from static/legacy/.")
    size_bytes = models.PositiveIntegerField(default=0)
    order = models.PositiveSmallIntegerField(default=99)

    class Meta:
        ordering = ["order", "name"]

    def __str__(self):
        return self.name

    @property
    def url(self):
        if self.file:
            return self.file.url
        return self.external_path

    @property
    def size_display(self):
        return human_size(self.size_bytes)


class StudentActivity(models.Model):
    """Per-student, per-lesson state: bookmark, completion, reading progress."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, related_name="activity",
                              on_delete=models.CASCADE)
    lesson = models.ForeignKey(Lesson, related_name="activity", on_delete=models.CASCADE)
    bookmarked = models.BooleanField(default=False)
    completed = models.BooleanField(default=False)
    progress_pct = models.PositiveSmallIntegerField(default=0)
    last_viewed = models.DateTimeField(auto_now=True)
    view_count = models.PositiveIntegerField(default=0)

    class Meta:
        unique_together = [("user", "lesson")]
        ordering = ["-last_viewed"]
        verbose_name_plural = "student activity"

    def __str__(self):
        return f"{self.user} · {self.lesson}"


class LessonUpload(models.Model):
    """
    Audit trail for the "upload new notes" feature. The uploaded HTML file is
    kept alongside the Lesson it produced, so a trainer can always see exactly
    what was submitted and when.
    """

    STATUS_PENDING = "pending"
    STATUS_PUBLISHED = "published"
    STATUS_REJECTED = "rejected"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending review"),
        (STATUS_PUBLISHED, "Published"),
        (STATUS_REJECTED, "Rejected"),
    ]

    module = models.ForeignKey(Module, related_name="uploads", on_delete=models.CASCADE)
    unit = models.ForeignKey(Unit, related_name="uploads", null=True, blank=True,
                              on_delete=models.SET_NULL)
    new_unit_title = models.CharField(max_length=220, blank=True)

    source_html = models.FileField(
        upload_to=upload_lesson_source_path, max_length=400,
        validators=[FileExtensionValidator(allowed_extensions=["html", "htm"])])
    original_filename = models.CharField(max_length=260, blank=True)

    lesson = models.OneToOneField(Lesson, null=True, blank=True,
                                   on_delete=models.SET_NULL, related_name="upload_record")
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=STATUS_PENDING)
    note = models.CharField(max_length=400, blank=True)

    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                     related_name="lesson_uploads")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.original_filename} → {self.module.code}"


# --------------------------------------------------------------------------- #
# Module notes — the Trainer-managed PDF / HTML notes feature
# --------------------------------------------------------------------------- #

class ModuleNoteQuerySet(models.QuerySet):
    def published(self):
        return self.filter(is_published=True)


class ModuleNote(models.Model):
    """
    A titled PDF or HTML notes file filed under a module.

    Lifecycle: a Trainer uploads it (draft or published straight away), can
    edit the title/module, replace the file, publish / unpublish it, and delete
    it. Only published notes are visible to students; drafts are visible to
    their author and to Administrators.

    For HTML notes the upload is also parsed by `html_processing` into a
    sanitised fragment (`content_html`) that the reader page renders inline.
    The original file is kept and offered as a download, but is never served
    as a live page from our origin.
    """

    TYPE_PDF = "pdf"
    TYPE_HTML = "html"
    TYPE_CHOICES = [(TYPE_PDF, "PDF"), (TYPE_HTML, "HTML")]

    module = models.ForeignKey(Module, related_name="notes", on_delete=models.CASCADE)
    title = models.CharField(max_length=200)

    file = models.FileField(
        upload_to=upload_note_path, storage=private_storage, max_length=400,
        validators=[FileExtensionValidator(allowed_extensions=["pdf", "html", "htm"])])
    file_type = models.CharField(max_length=4, choices=TYPE_CHOICES)
    original_filename = models.CharField(max_length=260, blank=True)
    size_bytes = models.PositiveIntegerField(default=0)

    # Populated for HTML notes only.
    content_html = models.TextField(blank=True,
                                     help_text="Sanitised HTML fragment (HTML notes only).")
    search_text = models.TextField(blank=True)
    headings_json = models.JSONField(default=list, blank=True)
    words = models.PositiveIntegerField(default=0)
    minutes = models.PositiveSmallIntegerField(default=0)

    is_published = models.BooleanField(default=False)
    published_at = models.DateTimeField(null=True, blank=True)

    # SET_NULL, not CASCADE: removing a Trainer's account must not silently
    # delete the notes students are reading. Orphaned notes stay manageable by
    # an Administrator.
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="module_notes")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = ModuleNoteQuerySet.as_manager()

    class Meta:
        ordering = ["-updated_at"]
        indexes = [models.Index(fields=["is_published", "-published_at"])]

    def __str__(self):
        return f"{self.title} ({self.module.code})"

    # -- state -------------------------------------------------------------
    def publish(self):
        self.is_published = True
        self.published_at = timezone.now()

    def unpublish(self):
        self.is_published = False

    # -- helpers -----------------------------------------------------------
    @property
    def is_pdf(self):
        return self.file_type == self.TYPE_PDF

    @property
    def is_html(self):
        return self.file_type == self.TYPE_HTML

    @property
    def size_display(self):
        return human_size(self.size_bytes)

    def get_absolute_url(self):
        return reverse("core:note_detail", args=[self.pk])

    def get_file_url(self):
        return reverse("core:note_file", args=[self.pk])

    def get_download_url(self):
        return reverse("core:note_file", args=[self.pk]) + "?download=1"

    def download_filename(self):
        stem = re.sub(r"[^\w\s-]", "", f"{self.module.code}_{self.title}").strip()
        stem = re.sub(r"\s+", "_", stem)[:110] or "notes"
        return f"{stem}.{'pdf' if self.is_pdf else 'html'}"


@receiver(post_delete, sender=ModuleNote)
def _remove_note_file(sender, instance, **kwargs):
    """Deleting a note (or its module) must not leave the file behind on disk."""
    if instance.file:
        instance.file.delete(save=False)
