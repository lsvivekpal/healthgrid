from rest_framework.permissions import SAFE_METHODS, BasePermission


class IsStaffOrReadOnly(BasePermission):
    """Authenticated users can view; staff can edit; only admins can delete."""

    def has_permission(self, request, view):
        if request.method == "DELETE":
            return bool(request.user and request.user.is_authenticated and request.user.is_active
                        and request.user.is_staff and request.user.is_superuser)
        if request.method in SAFE_METHODS:
            return bool(request.user and request.user.is_authenticated)
        return bool(request.user and request.user.is_authenticated and request.user.is_staff)
