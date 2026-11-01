from rest_framework.permissions import BasePermission


class HasShop(BasePermission):
    """The signed-in user must own a shop (every normal account does; a bare admin account does not)."""

    message = "This account has no shop."

    def has_permission(self, request, view):
        user = request.user
        shop = getattr(user, "shop", None) if user and user.is_authenticated else None
        if shop is None:
            return False
        from . import plans

        plans.downgrade_if_due(shop)  # a Pro that has run out is switched back to Free on the very next request
        return True
