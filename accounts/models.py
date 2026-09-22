from django.conf import settings
from django.db import models


class Profile(models.Model):
    """One-to-one extension of the built-in User for platform-specific fields."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                 related_name="profile")
    trade = models.ForeignKey("core.Trade", null=True, blank=True,
                               on_delete=models.SET_NULL, related_name="+",
                               help_text="A student's current level, for a tailored dashboard.")
    trainer_modules = models.ManyToManyField(
        "core.Module", blank=True, related_name="trainers",
        help_text="Modules this Trainer may add notes to. Empty means none — a Trainer can also register their own modules when adding notes.")

    def __str__(self):
        return str(self.user)
