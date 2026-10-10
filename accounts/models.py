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


class LoginFailure(models.Model):
    """One failed sign-in. Used only to pause sign-in after repeated failures (see accounts/throttle.py)."""

    username = models.CharField(max_length=150, db_index=True)
    ip = models.GenericIPAddressField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    def __str__(self):
        return f"{self.username} @ {self.created_at:%Y-%m-%d %H:%M}"
