from django import template

from ..security import mask_endpoint


register = template.Library()


@register.filter
def masked_endpoint(value):
    return mask_endpoint(value)

