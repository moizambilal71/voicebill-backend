from rest_framework.authentication import SessionAuthentication


class SessionAuth(SessionAuthentication):
    """Session cookie sign-in. The `authenticate_header` makes "not signed in" a 401 (not 403) so the app
    knows to show the login page."""

    def authenticate_header(self, request):
        return "Session"


class CsrfAlways(SessionAuth):
    """For the sign-in/sign-up endpoints: demand a valid CSRF token even though nobody is signed in yet
    (stops other websites from silently signing a visitor into an attacker's account)."""

    def authenticate(self, request):
        self.enforce_csrf(request)
        return super().authenticate(request)
