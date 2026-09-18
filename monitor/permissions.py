from rest_framework.permissions import SAFE_METHODS, BasePermission

from .models import RDSInstance, ReplicationSlotAccess, UserInstanceAccess, UserInstanceAccessScope


def instance_role(user, instance):
    if not (user and user.is_authenticated and user.is_active):
        return None
    if user.is_superuser:
        return "administrator"
    grant = UserInstanceAccess.objects.filter(user=user, instance=instance).values_list("role", flat=True).first()
    if grant:
        return grant
    # Preserve access for accounts created before per-instance grants existed.
    # Saving assignments for that account creates an explicit scope.
    if UserInstanceAccessScope.objects.filter(user=user).exists() or UserInstanceAccess.objects.filter(user=user).exists():
        return None
    return "operator" if user.is_staff else "readonly"


def can_view_instance(user, instance):
    return instance_role(user, instance) is not None


def can_operate_instance(user, instance):
    return instance_role(user, instance) in {"administrator", "operator"}


def accessible_instances(user):
    queryset = RDSInstance.objects.filter(is_active=True)
    if user.is_superuser or not (
        UserInstanceAccessScope.objects.filter(user=user).exists()
        or UserInstanceAccess.objects.filter(user=user).exists()
    ):
        return queryset
    return queryset.filter(user_access_grants__user=user).distinct()


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
