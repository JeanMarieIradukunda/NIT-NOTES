from django import forms
from django.core.validators import FileExtensionValidator
from django.db import models

from .models import Activity, Module, Unit


class LessonUploadForm(forms.Form):
    """
    The per-module "Upload new lesson notes" form. A Trainer either files the
    new lesson under an existing learning outcome / topic, or types a new one
    on the spot — matching how the original site was actually organised,
    where not every module has formal learning outcomes.
    """

    source_html = forms.FileField(
        label="Lesson notes (.html)",
        validators=[FileExtensionValidator(allowed_extensions=["html", "htm"])],
        widget=forms.ClearableFileInput(attrs={"accept": ".html,.htm", "class": "form-control"}),
        help_text="An HTML file exported from Word, OneNote, or written directly.",
    )
    unit = forms.ModelChoiceField(
        queryset=Unit.objects.none(), required=False,
        label="File under", empty_label="— create a new section below —",
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    new_unit_title = forms.CharField(
        max_length=220, required=False, label="New section title",
        widget=forms.TextInput(attrs={
            "class": "form-control",
            "placeholder": "e.g. “Learning outcome 3” or “Recursion”"}),
    )
    order = forms.IntegerField(
        required=False, min_value=1, initial=99, label="Position (optional)",
        widget=forms.NumberInput(attrs={"class": "form-control", "style": "max-width:8rem"}),
    )

    def __init__(self, *args, module: Module, **kwargs):
        super().__init__(*args, **kwargs)
        self.module = module
        self.fields["unit"].queryset = module.units.order_by("order", "title")
        self.fields["new_unit_title"].widget.attrs.setdefault("class", "form-control")

    def clean(self):
        cleaned = super().clean()
        if not cleaned.get("unit") and not cleaned.get("new_unit_title"):
            raise forms.ValidationError(
                "Choose an existing section, or type a title for a new one.")
        return cleaned


# --------------------------------------------------------------------------- #
# Module notes (Trainer-managed PDF / HTML uploads)
# --------------------------------------------------------------------------- #

import os
import re

from bs4 import UnicodeDammit
from django.conf import settings
from django.db import transaction
from django.utils.text import Truncator

from .html_processing import LessonContentError, process_lesson_html
from .models import ModuleNote, Trade

NOTE_FIGURE_NOTE = "[Figure: {alt} — image is not embedded in the uploaded file]"
ALLOWED_NOTE_EXTENSIONS = {".pdf": ModuleNote.TYPE_PDF,
                           ".html": ModuleNote.TYPE_HTML,
                           ".htm": ModuleNote.TYPE_HTML}


class _ModuleChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        level = obj.trade.short_name or obj.trade.name
        return f"{obj.code} — {obj.name}  ·  {level}"


def module_key_from_code(code: str) -> str:
    """Folder-style module key from a displayed code: 'GEN CP-302' -> 'GEN-CP-302'."""
    return re.sub(r"[^A-Za-z0-9_-]+", "-", code.strip()).strip("-").upper()


def _code_token(text: str) -> str:
    """Compare codes loosely: 'gen cp-302' == 'GENCP302'."""
    return re.sub(r"[^A-Za-z0-9]+", "", text or "").upper()


def _name_token(text: str) -> str:
    """Compare names loosely: ignore case and repeated spaces."""
    return re.sub(r"\s+", " ", text or "").strip().casefold()


class _ExistingModuleForm(forms.Form):
    """
    The "Module" step shared by the notes and activity forms.

    A module can only ever be one that already exists — nothing here creates
    one (administrators add modules under Curriculum). The person either picks
    from their list, or types the module's CODE and NAME, which are compared
    with the existing modules. Only when both match one module does the form
    go on to publish or save a draft under it.
    """

    module = _ModuleChoiceField(
        queryset=Module.objects.none(), required=False,
        empty_label="Select a module…", label="Module",
        widget=forms.Select(attrs={"class": "form-select"}))
    lookup_code = forms.CharField(
        max_length=30, required=False, label="Module code",
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "e.g. GENCP302",
                                      "autocomplete": "off"}))
    lookup_name = forms.CharField(
        max_length=200, required=False, label="Module name",
        widget=forms.TextInput(attrs={"class": "form-control", "autocomplete": "off",
                                      "placeholder": "e.g. C Programming Fundamentals"}))

    def _resolve_module(self, cleaned):
        """
        Return the existing Module this submission refers to, or None after
        adding an explanatory error. A module picked from the list wins;
        otherwise the typed code + name are compared with every module.
        """
        picked = cleaned.get("module")
        if picked:
            return picked

        code = (cleaned.get("lookup_code") or "").strip()
        name = (cleaned.get("lookup_name") or "").strip()
        if not code and not name:
            self.add_error("module", "Select a module, or type its code and name to find it.")
            return None
        if not code:
            self.add_error("lookup_code", "Enter the module code.")
        if not name:
            self.add_error("lookup_name", "Enter the module name.")
        if not (code and name):
            return None

        code_tok, name_tok = _code_token(code), _name_token(name)
        if not code_tok:
            self.add_error("lookup_code", "Use letters and numbers in the module code, e.g. GENCP302.")
            return None

        everything = list(Module.objects.select_related("trade"))
        by_code = [m for m in everything if code_tok in (_code_token(m.code), _code_token(m.key))]
        by_name = [m for m in everything if _name_token(m.name) == name_tok]
        both = [m for m in by_code if m in by_name]

        if both:
            allowed = set(self.fields["module"].queryset.values_list("pk", flat=True))
            usable = [m for m in both if m.pk in allowed]
            if len(usable) == 1:
                return usable[0]
            if len(usable) > 1:
                self.add_error("module", "More than one module has this code and name — "
                                         "select the right one from the list instead.")
                return None
            self.add_error("lookup_code",
                           f"{both[0].code} exists, but it isn't assigned to you. "
                           "Ask an administrator to assign it to you.")
            return None

        if by_code:
            self.add_error("lookup_name",
                           f"The code {by_code[0].code} belongs to “{by_code[0].name}” — "
                           "the name you entered doesn't match.")
        elif by_name:
            self.add_error("lookup_code",
                           f"“{by_name[0].name}” exists with the code {by_name[0].code} — "
                           "the code you entered doesn't match.")
        else:
            self.add_error("lookup_code",
                           "No module with this code and name exists. Notes and activities can only "
                           "be added to existing modules — ask an administrator to add it under Curriculum.")
        return None


class ModuleNoteForm(_ExistingModuleForm):
    """
    Add or edit a module note.

    Module: an EXISTING one — picked from your list, or found by typing its
    code and name (see `_ExistingModuleForm`). Title: what students will see.
    File: a PDF or an HTML file.
    Publishing is decided by which button is pressed (`intent`), handled in
    `apply()`.
    """

    title = forms.CharField(
        max_length=200, label="Title",
        widget=forms.TextInput(attrs={"class": "form-control",
                                      "placeholder": "e.g. Loops and arrays — lesson notes"}))
    file = forms.FileField(
        required=False, label="Notes file",
        widget=forms.ClearableFileInput(attrs={"class": "form-control",
                                               "accept": ".pdf,.html,.htm"}))

    def __init__(self, *args, user, note=None, **kwargs):
        super().__init__(*args, **kwargs)
        from accounts.roles import note_modules_for

        self.user = user
        self.note = note
        self._parsed_html = None
        self._file_type = None
        self.fields["module"].queryset = note_modules_for(user)

        if note is not None:
            # Editing: the current module is always selectable, even if the
            # author was later unassigned from it (an Administrator editing
            # another Trainer's note also needs it in the list).
            self.fields["module"].queryset = (
                self.fields["module"].queryset | Module.objects.filter(pk=note.module_id)
            ).distinct().select_related("trade").order_by("trade__order", "order", "code")
            self.fields["file"].help_text = "Leave empty to keep the current file."
        self.max_bytes = settings.MAX_NOTE_SIZE_MB * 1024 * 1024

    def full_clean(self):
        super().full_clean()
        # Flag failed inputs for styling and assistive technology.
        for name in self.errors:
            field = self.fields.get(name)
            if field is not None:
                attrs = field.widget.attrs
                attrs["class"] = (attrs.get("class", "") + " is-invalid").strip()
                attrs["aria-invalid"] = "true"

    # -- fields ------------------------------------------------------------
    def clean_title(self):
        title = re.sub(r"\s+", " ", self.cleaned_data["title"]).strip()
        if not title:
            raise forms.ValidationError("Enter a title for these notes.")
        return title

    def clean_file(self):
        f = self.cleaned_data.get("file")
        if not f:
            return None

        ext = os.path.splitext(f.name)[1].lower()
        if ext not in ALLOWED_NOTE_EXTENSIONS:
            raise forms.ValidationError("Upload a PDF (.pdf) or HTML (.html, .htm) file.")
        if f.size == 0:
            raise forms.ValidationError("That file is empty.")
        if f.size > self.max_bytes:
            raise forms.ValidationError(
                f"That file is {f.size / 1048576:.1f} MB — the limit is "
                f"{settings.MAX_NOTE_SIZE_MB} MB.")

        head = f.read(2048)
        f.seek(0)
        is_pdf_bytes = b"%PDF-" in head[:1024]
        file_type = ALLOWED_NOTE_EXTENSIONS[ext]

        # Judge the file by its content, not just its name.
        if file_type == ModuleNote.TYPE_PDF and not is_pdf_bytes:
            raise forms.ValidationError(
                "This file isn't a valid PDF, even though its name ends in .pdf.")
        if file_type == ModuleNote.TYPE_HTML:
            if is_pdf_bytes:
                raise forms.ValidationError(
                    "This is a PDF with an .html file name. Rename it to .pdf and upload it again.")
            raw = f.read()
            f.seek(0)
            text = UnicodeDammit(raw, is_html=True).unicode_markup or ""
            try:
                self._parsed_html = process_lesson_html(
                    text, max_bytes=self.max_bytes, figure_note=NOTE_FIGURE_NOTE)
            except LessonContentError as exc:
                raise forms.ValidationError(str(exc))

        self._file_type = file_type
        return f

    # -- whole form --------------------------------------------------------
    def clean(self):
        cleaned = super().clean()

        if self.note is None and not cleaned.get("file") and "file" not in self.errors:
            self.add_error("file", "Choose a PDF or HTML file to upload.")

        module = self._resolve_module(cleaned)
        if module is not None:
            cleaned["module"] = module
        return cleaned

    # -- persistence -------------------------------------------------------
    @transaction.atomic
    def apply(self, intent: str) -> ModuleNote:
        """
        Create or update the note. `intent` is 'draft', 'publish' or 'save'
        ('save' leaves the published/draft state as it is).
        """
        data = self.cleaned_data
        module = data["module"]          # always an existing module (see _resolve_module)

        note = self.note or ModuleNote(uploaded_by=self.user)
        note.module = module
        note.title = data["title"]

        old_file = None
        upload = data.get("file")
        if upload:
            if note.pk and note.file:
                old_file = (note.file.storage, note.file.name)
            note.file = upload
            note.file_type = self._file_type
            note.original_filename = os.path.basename(upload.name)[:260]
            note.size_bytes = upload.size
            parsed = self._parsed_html
            if parsed:
                note.content_html = parsed["fragment"]
                note.search_text = parsed["text"]
                note.headings_json = parsed["headings"]
                note.words = parsed["words"]
                note.minutes = parsed["minutes"]
            else:
                note.content_html, note.search_text = "", ""
                note.headings_json, note.words, note.minutes = [], 0, 0

        if intent == "publish" and not note.is_published:
            note.publish()
        elif intent == "draft":
            note.unpublish()

        note.save()

        if old_file:
            storage, name = old_file
            transaction.on_commit(lambda: storage.delete(name))
        return note


# --------------------------------------------------------------------------- #
# Activities (Module + Topic + a single HTML/PDF document, no Lesson needed)
# --------------------------------------------------------------------------- #

ALLOWED_ACTIVITY_EXTENSIONS = {".pdf": Activity.TYPE_PDF, ".html": Activity.TYPE_HTML,
                               ".htm": Activity.TYPE_HTML}


class ActivityForm(_ExistingModuleForm):
    """
    Add or edit an Activity. Only an EXISTING module (picked, or found by its
    code and name — see `_ExistingModuleForm`) and the document are required —
    title is optional (defaults to the uploaded file's name), and publishing
    is decided by which button is pressed (`intent`), handled in `apply()`,
    exactly like module notes.

    Activities are still filed under a Topic (Unit) internally — `apply()`
    files them under that module's "General" topic automatically, via
    `Unit.get_or_create_general()` — but a Trainer publishing an activity no
    longer has to choose one.
    """

    title = forms.CharField(
        max_length=220, required=False, label="Title",
        widget=forms.TextInput(attrs={"class": "form-control",
                                      "placeholder": "Optional — defaults to the file name"}))
    document = forms.FileField(
        required=False, label="Activity document",
        widget=forms.ClearableFileInput(attrs={"class": "form-control",
                                               "accept": ".pdf,.html,.htm"}))

    def __init__(self, *args, user, activity=None, **kwargs):
        super().__init__(*args, **kwargs)
        from accounts.roles import activity_modules_for

        self.user = user
        self.activity = activity
        modules = activity_modules_for(user)
        self.fields["module"].queryset = modules

        if activity is not None:
            # Editing: the current module stays selectable even if the author
            # was later unassigned from it (an Administrator editing another
            # Trainer's activity also needs it in the list).
            self.fields["module"].queryset = (
                self.fields["module"].queryset | Module.objects.filter(pk=activity.module_id)
            ).distinct().select_related("trade").order_by("trade__order", "order", "code")
            self.fields["document"].help_text = "Leave empty to keep the current file."
            self.fields["title"].help_text = "Leave empty to keep the current title."
        self.max_bytes = settings.MAX_ACTIVITY_SIZE_MB * 1024 * 1024

    def full_clean(self):
        super().full_clean()
        # Flag failed inputs for styling and assistive technology.
        for name in self.errors:
            field = self.fields.get(name)
            if field is not None:
                attrs = field.widget.attrs
                attrs["class"] = (attrs.get("class", "") + " is-invalid").strip()
                attrs["aria-invalid"] = "true"

    # -- fields ------------------------------------------------------------
    def clean_title(self):
        return re.sub(r"\s+", " ", self.cleaned_data.get("title", "")).strip()

    def clean_document(self):
        f = self.cleaned_data.get("document")
        if not f:
            return None

        ext = os.path.splitext(f.name)[1].lower()
        if ext not in ALLOWED_ACTIVITY_EXTENSIONS:
            raise forms.ValidationError("Upload an HTML (.html/.htm) or PDF (.pdf) file.")
        if f.size == 0:
            raise forms.ValidationError("That file is empty.")
        if f.size > self.max_bytes:
            raise forms.ValidationError(
                f"That file is {f.size / 1048576:.1f} MB — the limit is "
                f"{settings.MAX_ACTIVITY_SIZE_MB} MB.")

        head = f.read(2048)
        f.seek(0)
        is_pdf_bytes = b"%PDF-" in head[:1024]
        file_type = ALLOWED_ACTIVITY_EXTENSIONS[ext]

        # Judge the file by its content, not just its name.
        if file_type == Activity.TYPE_PDF and not is_pdf_bytes:
            raise forms.ValidationError(
                "This file isn't a valid PDF, even though its name ends in .pdf.")
        if file_type == Activity.TYPE_HTML and is_pdf_bytes:
            raise forms.ValidationError(
                "This is a PDF with an .html file name. Rename it to .pdf and upload it again.")
        return f

    # -- whole form --------------------------------------------------------
    def clean(self):
        cleaned = super().clean()
        if self.activity is None and not cleaned.get("document") and "document" not in self.errors:
            self.add_error("document", "Choose a PDF or HTML file to upload.")

        module = self._resolve_module(cleaned)
        if module is not None:
            cleaned["module"] = module
        return cleaned

    # -- persistence -------------------------------------------------------
    @transaction.atomic
    def apply(self, intent: str) -> Activity:
        """
        Create or update the activity. `intent` is 'draft', 'publish' or 'save'
        ('save' leaves the published/draft state as it is).
        """
        data = self.cleaned_data
        activity = self.activity or Activity(uploaded_by=self.user)
        module = data["module"]
        if not activity.pk or activity.module_id != module.pk:
            # New activity, or moved to a different module: file it under
            # that module's catch-all topic. An edit that keeps the same
            # module keeps whatever topic the activity already had.
            activity.topic = Unit.get_or_create_general(module)
        activity.module = module

        title = data.get("title") or ""
        if title or not activity.pk:
            # A blank title on create is fine — Activity.save() derives one
            # from the file name. A blank title on edit just keeps the
            # existing one (see the field's help text).
            activity.title = title

        old_file = None
        upload = data.get("document")
        if upload:
            if activity.pk and activity.document:
                old_file = (activity.document.storage, activity.document.name)
            activity.document = upload
            # document_type / original_filename / size_bytes are derived
            # automatically from the upload in Activity.save().

        if intent == "publish":
            activity.is_published = True
        elif intent == "draft":
            activity.is_published = False
        # 'save' leaves is_published as it is.

        activity.full_clean()
        activity.save()

        if old_file:
            storage, name = old_file
            transaction.on_commit(lambda: storage.delete(name))
        return activity


# --------------------------------------------------------------------------- #
# Curriculum (Trades and Modules) — Administrator only, in-app replacement
# for what used to be managed at /admin/core/trade/ and /admin/core/module/.
# --------------------------------------------------------------------------- #

class TradeForm(forms.ModelForm):
    """Add or edit a Trade (a level, e.g. "Level 4 — National IT", or the
    past-papers archive)."""

    class Meta:
        model = Trade
        fields = ["name", "short_name", "key", "summary", "kind", "order"]
        labels = {"key": "URL key", "kind": "Type"}
        help_texts = {
            "key": "Short, unique, and used in the level's web address — "
                   "letters, numbers and hyphens only, e.g. L4, L5NIT.",
            "short_name": "A compact label for tight spaces, e.g. \"L4\" for "
                          "\"Level 4 — National IT\". Optional — falls back to the full name.",
            "order": "Levels are listed lowest number first.",
        }
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control",
                                           "placeholder": "e.g. Level 4 — National IT"}),
            "short_name": forms.TextInput(attrs={"class": "form-control",
                                                 "placeholder": "e.g. Level 4"}),
            "key": forms.TextInput(attrs={"class": "form-control", "placeholder": "e.g. L4"}),
            "summary": forms.TextInput(attrs={"class": "form-control",
                                              "placeholder": "One line describing this level"}),
            "kind": forms.Select(attrs={"class": "form-select"}),
            "order": forms.NumberInput(attrs={"class": "form-control", "min": 0}),
        }

    def clean_key(self):
        key = (self.cleaned_data.get("key") or "").strip().lower()
        if not re.match(r"^[a-z0-9-]+$", key):
            raise forms.ValidationError(
                "Use only lowercase letters, numbers and hyphens, e.g. l4 or l5nit.")
        clash = Trade.objects.filter(key__iexact=key).exclude(pk=self.instance.pk)
        if clash.exists():
            raise forms.ValidationError("A level with this URL key already exists.")
        return key


class ModuleForm(forms.ModelForm):
    """
    Add or edit a Module. The folder-style `key` used in the module's web
    address is derived automatically from the code, so nobody has to think
    about it twice. This is the only place modules are created.
    """

    class Meta:
        model = Module
        fields = ["trade", "code", "name", "order"]
        labels = {"trade": "Level"}
        help_texts = {"order": "Modules are listed lowest number first within their level."}
        widgets = {
            "trade": forms.Select(attrs={"class": "form-select"}),
            "code": forms.TextInput(attrs={"class": "form-control", "placeholder": "e.g. GENCP302"}),
            "name": forms.TextInput(attrs={"class": "form-control",
                                           "placeholder": "e.g. C Programming Fundamentals"}),
            "order": forms.NumberInput(attrs={"class": "form-control", "min": 0}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["trade"].queryset = Trade.objects.order_by("order", "name")

    def clean(self):
        cleaned = super().clean()
        trade, code = cleaned.get("trade"), (cleaned.get("code") or "").strip()
        if not (trade and code):
            return cleaned
        key = module_key_from_code(code)
        if not key:
            self.add_error("code", "Use letters and numbers in the module code, e.g. GENCP302.")
            return cleaned
        clash = Module.objects.filter(trade=trade).filter(
            models.Q(key__iexact=key) | models.Q(code__iexact=code)
        ).exclude(pk=self.instance.pk).first()
        if clash:
            self.add_error("code", f"{clash.code} already exists at this level.")
        cleaned["key"] = key
        return cleaned

    def save(self, commit=True):
        module = super().save(commit=False)
        module.key = self.cleaned_data["key"]
        if commit:
            module.save()
        return module
