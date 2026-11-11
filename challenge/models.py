import os
import uuid

from django.conf import settings
from django.db import models
from django.db.models import Max
from django.db.models.signals import post_delete
from django.dispatch import receiver
from django.urls import reverse
from django.utils.text import slugify

from core.models import Module, private_storage

from . import characters, images, render


def upload_card_path(instance, filename):
    """A random name, so nothing is guessable from the address or the disk."""
    ext = os.path.splitext(filename)[1].lower()
    return f"knowledge_cards/{instance.module.trade.key}/{instance.module.key}/{uuid.uuid4().hex}{ext}"


class CardQuerySet(models.QuerySet):
    def published(self):
        return self.filter(is_published=True)

    def in_order(self):
        return self.order_by("module__trade__order", "module__order", "module__code", "order", "number", "id")


class KnowledgeCard(models.Model):
    """
    One illustrated IT meme that teaches a single idea. Always filed under an existing module (its topic).

    The picture is either generated from the parts below (title, speech bubble, question, answer, explanation,
    character and mood) or is a finished 1080 x 1080 SVG the trainer uploaded (``custom_svg``, cleaned on upload).
    The text parts are always kept: they give the picture its alt text and make the card searchable.
    """

    module = models.ForeignKey(Module, related_name="knowledge_cards", on_delete=models.CASCADE)
    title = models.CharField(max_length=render.LIMITS["title"])
    character = models.CharField(max_length=12, choices=characters.CHARACTERS, default="laptop")
    mood = models.CharField(max_length=10, choices=characters.MOODS, default="happy")
    # The text parts are needed for a generated card. A card that is a finished picture of the trainer's own may leave
    # them empty (the title and module then give it its alt text).
    bubble = models.CharField("Speech bubble", max_length=render.LIMITS["bubble"], blank=True, default="")
    question = models.CharField(max_length=render.LIMITS["question"], blank=True, default="")
    answer = models.CharField(max_length=render.LIMITS["answer"], blank=True, default="")
    explanation = models.CharField(max_length=render.LIMITS["explanation"], blank=True, default="")
    custom_svg = models.TextField(blank=True, help_text="A finished SVG design, cleaned on upload. Empty = generated card.")
    # A finished PNG / JPG / WebP picture. Kept in the same file store as module notes (Vercel Blob when deployed).
    image = models.FileField(upload_to=upload_card_path, storage=private_storage, blank=True, max_length=400)

    number = models.PositiveSmallIntegerField(default=0, help_text="Card number within its module.")
    order = models.PositiveSmallIntegerField(default=99)
    is_published = models.BooleanField(default=True)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name="knowledge_cards")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = CardQuerySet.as_manager()

    class Meta:
        ordering = ["module__trade__order", "module__order", "order", "number", "id"]
        indexes = [models.Index(fields=["is_published", "module"])]

    def __str__(self):
        return f"{self.title} ({self.module.code})"

    def save(self, *args, **kwargs):
        if not self.number:
            top = KnowledgeCard.objects.filter(module_id=self.module_id).aggregate(m=Max("number"))["m"] or 0
            self.number = top + 1
        super().save(*args, **kwargs)

    # -- the picture ---------------------------------------------------------
    def parts(self):
        return {"title": self.title, "bubble": self.bubble, "question": self.question, "answer": self.answer,
                "explanation": self.explanation, "character": self.character, "mood": self.mood,
                "module_code": self.module.code, "module_name": self.module.name, "number": self.number,
                "palette": self.module_id or 0}

    def svg(self):
        """The card as SVG text (generated or the trainer's own SVG). A picture card has none."""
        if self.custom_svg:
            return self.custom_svg
        from core.context_processors import brand
        return render.render_card(self.parts(), brand(None)["BRAND"]["institution"])

    @property
    def has_custom_design(self):
        """The trainer supplied the picture (an SVG design or a PNG / JPG / WebP file)."""
        return bool(self.custom_svg or self.image)

    @property
    def picture_kind(self):
        return "image" if self.image else "svg" if self.custom_svg else "generated"

    @property
    def image_ext(self):
        return os.path.splitext(self.image.name)[1].lower() if self.image else ""

    @property
    def image_content_type(self):
        return images.CONTENT_TYPES.get(self.image_ext, "application/octet-stream")

    @property
    def picture_label(self):
        """Short name of the downloadable file type: PNG, JPG, WebP or SVG."""
        return images.LABELS.get(self.image_ext, "SVG")

    @property
    def alt_text(self):
        if not (self.bubble and self.question and self.answer):
            return f"{self.title}. An IT Knowledge Challenge card about {self.module.name}."
        return render.alt_text(self.parts())

    # -- addresses -----------------------------------------------------------
    def get_absolute_url(self):
        return reverse("challenge:card_detail", args=[self.pk])

    @property
    def picture_url(self):
        """The card's picture, whatever kind it is. Includes the last-changed time so browsers can cache it for a long
        time and still see edits at once."""
        return f"{reverse('challenge:card_picture', args=[self.pk])}?v={int(self.updated_at.timestamp())}"

    svg_url = picture_url          # the name the templates and scripts already use

    @property
    def download_name(self):
        ext = self.image_ext or ".svg"
        return f"IT-Knowledge-Challenge-{self.module.code}-{self.number:02d}-{slugify(self.title)[:40] or 'card'}{ext}"


@receiver(post_delete, sender=KnowledgeCard)
def _remove_picture_file(sender, instance, **kwargs):
    """Deleting a card (or its module) also deletes its uploaded picture from the file store."""
    if instance.image:
        try:
            instance.image.storage.delete(instance.image.name)
        except Exception:                                          # a missing or unreachable file must not block a delete
            pass
