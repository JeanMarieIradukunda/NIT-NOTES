"""Optional artwork: use an image only when its file is really there, so a missing file never breaks a page."""

from django import template
from django.contrib.staticfiles import finders
from django.contrib.staticfiles.storage import staticfiles_storage

register = template.Library()


@register.simple_tag
def static_if_exists(name):
    """
    The URL of a static file, or '' when it has not been added yet.

    {% static %} on a missing file raises an error with the hashed-filename storage used in production, which would
    turn a forgotten image into a broken page. This returns '' instead, and the page falls back to its plain design.
    """
    try:
        if not (finders.find(name) or staticfiles_storage.exists(name)):
            return ""
        return staticfiles_storage.url(name)
    except Exception:
        return ""
