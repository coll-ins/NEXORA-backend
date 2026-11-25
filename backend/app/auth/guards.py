"""Route guards. Platform owner is read from the database on every request."""

from functools import wraps

from flask import g

from app.errors import error_response
from app.security.tenant import AccessDenied, current_user


def platform_owner_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        try:
            user = current_user()
        except AccessDenied as exc:
            return error_response(exc.status, exc.message)
        from app.security.permissions import is_platform_owner

        if not is_platform_owner(user):
            return error_response(403, "Platform owner access required.")
        g.user = user
        return view(*args, **kwargs)

    return wrapper


def access_token_required(view):
    """Any signed-in user. Sets g.user. No SACCO or suspension check, on purpose."""

    @wraps(view)
    def wrapper(*args, **kwargs):
        try:
            g.user = current_user()
        except AccessDenied as exc:
            return error_response(exc.status, exc.message)
        return view(*args, **kwargs)

    return wrapper
