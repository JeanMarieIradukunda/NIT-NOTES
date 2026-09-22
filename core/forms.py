from django import forms
from django.core.validators import FileExtensionValidator
from django.db import models

from .models import Module, Unit


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


class ModuleNoteForm(forms.Form):
    """
    Add or edit a module note.

    Module: pick one of your modules, or register a new one by entering its code,
    name and level. Title: what students will see. File: a PDF or an HTML file.
    Publishing is decided by which button is pressed (`intent`), handled in
    `apply()`.
    """

    module = _ModuleChoiceField(
        queryset=Module.objects.none(), required=False,
        empty_label="Select a module…", label="Module",
        widget=forms.Select(attrs={"class": "form-select"}))
    new_module_code = forms.CharField(
        max_length=30, required=False, label="Module code",
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "e.g. GENCP302",
                                      "autocomplete": "off"}))
    new_module_name = forms.CharField(
        max_length=200, required=False, label="Module name",
        widget=forms.TextInput(attrs={"class": "form-control",
                                      "placeholder": "e.g. C Programming Fundamentals"}))
    new_module_level = forms.ModelChoiceField(
        queryset=Trade.objects.none(), required=False, label="Level",
        empty_label="Select a level…",
        widget=forms.Select(attrs={"class": "form-select"}))

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
        self.fields["new_module_level"].queryset = Trade.objects.order_by("order", "name")

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
        module = cleaned.get("module")
        code = (cleaned.get("new_module_code") or "").strip()
        name = (cleaned.get("new_module_name") or "").strip()
        level = cleaned.get("new_module_level")

        if self.note is None and not cleaned.get("file") and "file" not in self.errors:
            self.add_error("file", "Choose a PDF or HTML file to upload.")

        if module:
            return cleaned

        if not (code or name or level):
            self.add_error("module", "Select a module, or enter the code, name and level of a new one.")
            return cleaned

        if not code:
            self.add_error("new_module_code", "Enter the module code.")
        if not name:
            self.add_error("new_module_name", "Enter the module name.")
        if not level:
            self.add_error("new_module_level", "Choose the level this module belongs to.")
        if not (code and name and level):
            return cleaned

        key = module_key_from_code(code)
        if not key:
            self.add_error("new_module_code",
                           "Use letters and numbers in the module code, e.g. GENCP302.")
            return cleaned

        clash = Module.objects.filter(trade=level).filter(
            models.Q(key__iexact=key) | models.Q(code__iexact=code)).first()
        if clash:
            if self.fields["module"].queryset.filter(pk=clash.pk).exists():
                msg = (f"{clash.code} already exists — pick it from the Module list "
                       "instead of entering it again.")
            else:
                msg = (f"A module with the code {clash.code} already exists at this level "
                       "and isn't assigned to you. If you teach it, ask an administrator "
                       "to assign it to you.")
            self.add_error("new_module_code", msg)
        return cleaned

    # -- persistence -------------------------------------------------------
    @transaction.atomic
    def apply(self, intent: str) -> ModuleNote:
        """
        Create or update the note. `intent` is 'draft', 'publish' or 'save'
        ('save' leaves the published/draft state as it is).
        """
        from accounts.roles import is_admin

        data = self.cleaned_data
        module = data.get("module")
        if module is None:
            module = Module.objects.create(
                trade=data["new_module_level"],
                key=module_key_from_code(data["new_module_code"]),
                code=data["new_module_code"].strip(),
                name=data["new_module_name"].strip(),
            )
            # A Trainer who registers a module is assigned to it, so it shows up
            # in their Module list from now on.
            if not is_admin(self.user):
                self.user.profile.trainer_modules.add(module)

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
