from rest_framework.permissions import BasePermission


class HasShop(BasePermission):
    """The signed-in user must own a shop (every normal account does; a bare admin account does not)."""

    message = "This account has no shop."

    def has_permission(self, request, view):
        user = request.user
        return bool(user and user.is_authenticated and getattr(user, "shop", None) is not None)
