from rest_framework.permissions import SAFE_METHODS, BasePermission

from .models import ReplicationSlotAccess


def can_manage_replication_slot(user, action):
    if action not in {"drop", "terminate"}:
        return False
    if not (user.is_authenticated and user.is_active and user.is_staff):
        return False
    if user.is_superuser:
        return True
    try:
        access = user.replication_slot_access
    except ReplicationSlotAccess.DoesNotExist:
        return False
    return getattr(access, f"can_{action}")


class IsStaffOrReadOnly(BasePermission):
    """Authenticated users can view; staff can edit; only admins can delete."""

    def has_permission(self, request, view):
        if request.method == "DELETE":
            return bool(request.user and request.user.is_authenticated and request.user.is_active
                        and request.user.is_staff and request.user.is_superuser)
        if request.method in SAFE_METHODS:
            return bool(request.user and request.user.is_authenticated)
        return bool(request.user and request.user.is_authenticated and request.user.is_staff)
