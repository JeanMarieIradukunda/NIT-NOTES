from django import template

register = template.Library()


@register.filter
def dict_get(d, key):
    """Look up a dict value by key inside a template, where d[key] isn't allowed."""
    if not d:
        return None
    return d.get(key)
