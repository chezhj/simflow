"""
Custom filter code to enable access to environment variables
in templates
"""
import datetime
import os
from django import template

from django.conf import settings as conf_settings

register = template.Library()


@register.filter
def env(key):
    """Get environment key"""
    return os.environ.get(key, None)


@register.filter
def setting(key):
    """Get setting based on key"""

    return getattr(conf_settings, key, None)


@register.filter
def natural_duration(value):
    """
    Render a timedelta in words: "30 minutes", "1 hour", "2 hours 15 minutes".

    Added for the lockout page, which receives axes' cooloff as a timedelta.
    Rendering it directly gives "0:30:00", and hardcoding "30 minutes" in the
    template would silently lie the moment AXES_COOLOFF_TIME changed.

    Returns "" for anything that is not a timedelta, so a template referencing
    a missing context variable degrades to nothing rather than to "None".
    """
    if not isinstance(value, datetime.timedelta):
        return ""

    total = int(value.total_seconds())
    if total < 60:
        return f"{total} second{'' if total == 1 else 's'}"

    hours, remainder = divmod(total // 60, 60)
    parts = []
    if hours:
        parts.append(f"{hours} hour{'' if hours == 1 else 's'}")
    if remainder:
        parts.append(f"{remainder} minute{'' if remainder == 1 else 's'}")
    return " ".join(parts)
