from django import forms
from django.core.files.base import ContentFile

from accounts.roles import note_modules_for
from core.models import Module

from . import images, render
from .models import KnowledgeCard
from .svg_clean import SVGError, clean_svg


TEXT_PARTS = ("bubble", "question", "answer", "explanation")


def _text(name, label, rows=2, placeholder=""):
    # Required for a generated card (checked in clean()); optional when the trainer supplies the whole picture.
    return forms.CharField(
        label=label, max_length=render.LIMITS[name], required=False,
        widget=forms.Textarea(attrs={"class": "form-control", "rows": rows, "maxlength": render.LIMITS[name],
                                     "data-count": render.LIMITS[name], "placeholder": placeholder}))


class KnowledgeCardForm(forms.ModelForm):
    """Add or edit a card. The topic is one of the trainer's own modules (an administrator may pick any)."""

    module = forms.ModelChoiceField(queryset=Module.objects.none(), label="Topic (module)",
                                    widget=forms.Select(attrs={"class": "form-select"}))
    title = forms.CharField(label="Title", max_length=render.LIMITS["title"], widget=forms.TextInput(
        attrs={"class": "form-control", "maxlength": render.LIMITS["title"], "data-count": render.LIMITS["title"],
               "placeholder": "e.g. Ping Pong"}))
    bubble = _text("bubble", "Speech bubble (the funny line)", 2, "e.g. I send echo requests. Basically a polite stalker.")
    question = _text("question", "Question", 2, "e.g. Which command checks whether a device is reachable?")
    answer = _text("answer", "Answer", 2, "e.g. ping")
    explanation = _text("explanation", "Short explanation", 3, "One or two sentences on why.")
    svg_file = forms.FileField(
        required=False, label="Your own finished design (SVG)",
        widget=forms.ClearableFileInput(attrs={"class": "form-control", "accept": ".svg,image/svg+xml"}),
        help_text="A 1080 x 1080 SVG. It is cleaned of scripts and links.")
    remove_svg = forms.BooleanField(required=False, label="Remove my uploaded SVG design")
    image_file = forms.FileField(
        required=False, label="Your own meme picture (PNG, JPG or WebP)",
        widget=forms.ClearableFileInput(attrs={"class": "form-control", "accept": ".png,.jpg,.jpeg,.webp,image/png,image/jpeg,image/webp"}),
        help_text=f"Up to {images.size_text(images.MAX_IMAGE_BYTES)}. Square pictures (for example 1080 x 1080) fit the gallery best. "
                  "With your own picture the speech bubble, question, answer and explanation are optional.")
    remove_image = forms.BooleanField(required=False, label="Remove my uploaded picture")

    class Meta:
        model = KnowledgeCard
        fields = ["module", "title", "character", "mood", "bubble", "question", "answer", "explanation", "is_published"]
        widgets = {"character": forms.Select(attrs={"class": "form-select"}),
                   "mood": forms.Select(attrs={"class": "form-select"}),
                   "is_published": forms.CheckboxInput(attrs={"class": "form-check-input"})}
        labels = {"is_published": "Show in the gallery"}

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        qs = note_modules_for(user)
        if self.instance.pk:                                      # an administrator editing a colleague's card keeps its module
            qs = (qs | Module.objects.filter(pk=self.instance.module_id)).distinct()
        self.fields["module"].queryset = qs.select_related("trade").order_by("trade__order", "order", "code")
        self.fields["module"].label_from_instance = lambda m: f"{m.code} · {m.name}"
        self.cleaned_svg = None
        self.svg_warnings = []
        self.image_data = None
        self.image_ext = ""

    def clean_svg_file(self):
        f = self.cleaned_data.get("svg_file")
        if not f:
            return f
        try:
            self.cleaned_svg, self.svg_warnings = clean_svg(f.read())
        except SVGError as exc:
            raise forms.ValidationError(str(exc))
        return f

    def clean_image_file(self):
        f = self.cleaned_data.get("image_file")
        if not f:
            return f
        if f.size > images.MAX_IMAGE_BYTES:
            raise forms.ValidationError(
                f"This picture is {images.size_text(f.size)}. Please keep it under {images.size_text(images.MAX_IMAGE_BYTES)} "
                "(a free tool such as tinypng.com can shrink it).")
        data = f.read()
        ext = images.detect_image(data)
        if not ext:
            raise forms.ValidationError("That file is not a PNG, JPG or WebP picture.")
        self.image_data, self.image_ext = data, ext
        return f

    def clean(self):
        data = super().clean()
        if self.cleaned_svg and self.image_data:
            raise forms.ValidationError("Upload either a finished SVG design or a picture, not both.")
        keeps_svg = bool(self.instance.custom_svg) and not data.get("remove_svg") and not self.image_data
        keeps_image = bool(self.instance.image) and not data.get("remove_image") and not self.cleaned_svg
        if not (self.cleaned_svg or self.image_data or keeps_svg or keeps_image):
            for name in TEXT_PARTS:                       # a generated card needs all four parts
                if not (data.get(name) or "").strip():
                    self.add_error(name, "Required, unless you upload your own picture or design.")
        return data

    def _drop_image(self, card):
        if card.image:
            card.image.delete(save=False)

    def save(self, commit=True):
        card = super().save(commit=False)
        if self.cleaned_svg:
            self._drop_image(card)
            card.custom_svg = self.cleaned_svg
        elif self.image_data:
            self._drop_image(card)
            card.custom_svg = ""
            card.image.save(f"card{self.image_ext}", ContentFile(self.image_data), save=False)
        else:
            if self.cleaned_data.get("remove_svg"):
                card.custom_svg = ""
            if self.cleaned_data.get("remove_image"):
                self._drop_image(card)
        if commit:
            card.save()
        return card
