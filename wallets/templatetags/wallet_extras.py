from django import template

from decimal import Decimal

register = template.Library()


@register.filter
def get_item(mapping, key):
    try:
        return mapping.get(key)
    except Exception:
        return None


@register.filter
def fmt_dec(val):
    if val is None:
        return ""
    try:
        if isinstance(val, Decimal):
            return format(val, "f")
        if isinstance(val, (int, float)):
            return format(Decimal(str(val)), "f")
        s = str(val)
        return format(Decimal(s), "f")
    except Exception:
        try:
            return str(val)
        except Exception:
            return ""
