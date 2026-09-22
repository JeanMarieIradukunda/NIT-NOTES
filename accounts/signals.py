from django.contrib.auth import get_user_model
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import Profile

User = get_user_model()


@receiver(post_save, sender=User)
def create_profile(sender, instance, created, **kwargs):
    """
    Every user gets a Profile row. There is no default role to assign —
    Students never get accounts, and Trainer accounts are created directly
    by an Administrator, who assigns the Trainer group explicitly at
    creation time (see accounts.views.create_trainer).
    """
    if not created:
        return
    Profile.objects.get_or_create(user=instance)
