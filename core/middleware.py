"""Attaches convenience role flags to every request, computed once per request."""

from accounts.roles import is_admin, is_trainer


class RoleContextMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.is_trainer = is_trainer(request.user)
        request.is_admin_role = is_admin(request.user)
        return self.get_response(request)
