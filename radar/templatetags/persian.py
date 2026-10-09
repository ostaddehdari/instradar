from django import template
from django.utils import timezone
from django.utils.safestring import mark_safe
from radar.jalali import gregorian_to_jalali

register = template.Library()

FA_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


@register.filter
def fa_num(value):
    return str(value).translate(FA_DIGITS)


@register.filter
def jalali_datetime(value):
    if not value:
        return "—"
    local = timezone.localtime(value)
    year, month, day = gregorian_to_jalali(local.year, local.month, local.day)
    return f"{year:04d}/{month:02d}/{day:02d} {local:%H:%M}".translate(FA_DIGITS)


@register.filter
def jalali_date(value):
    if not value:
        return "—"
    if hasattr(value, "hour"):
        value = timezone.localtime(value).date()
    year, month, day = gregorian_to_jalali(value.year, value.month, value.day)
    return f"{year:04d}/{month:02d}/{day:02d}".translate(FA_DIGITS)


@register.simple_tag(takes_context=True)
def urlparams(context, **changes):
    query = context["request"].GET.copy()
    for key, value in changes.items():
        if value is None or value == "":
            query.pop(key, None)
        else:
            query[key] = value
    return mark_safe(query.urlencode())


@register.simple_tag(takes_context=True)
def sort_query(context, field):
    query = context["request"].GET.copy()
    current = query.get("sort")
    direction = query.get("dir", "desc")
    query["sort"] = field
    query["dir"] = "desc" if current == field and direction == "asc" else "asc"
    query.pop("page", None)
    return mark_safe(query.urlencode())
