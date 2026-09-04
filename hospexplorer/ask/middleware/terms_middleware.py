from django.shortcuts import redirect
from django.conf import settings
from django.urls import resolve
from ask.models import TermsAcceptance


class TermsAcceptanceMiddleware:
    EXEMPT_URL_NAMES = {"terms-accept", "terms-view"}

    EXEMPT_URL_PREFIXES = ("accounts/", "admin/")

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if self._requires_terms_check(request):
            current_version = settings.TERMS_VERSION

            # Check session cache first
            if request.session.get("terms_accepted_version") == current_version:
                return self.get_response(request)

            # Session miss, check DB if user has accepted the terms
            has_accepted = TermsAcceptance.objects.filter(
                user=request.user,
                terms_version=current_version,
            ).exists()

            if has_accepted:
                # Cache in session so we dont have to call the db again this session
                request.session["terms_accepted_version"] = current_version
            else:
                return redirect("ask:terms-accept")

        return self.get_response(request)

    def _requires_terms_check(self, request):
        if not hasattr(request, "user") or not request.user.is_authenticated:
            return False

        path = request.path
        app_root = getattr(settings, "APP_ROOT", "")

        for prefix in self.EXEMPT_URL_PREFIXES:
            full_prefix = f"/{app_root}{prefix}"
            if path.startswith(full_prefix):
                return False

        try:
            resolved = resolve(path)
            if resolved.url_name in self.EXEMPT_URL_NAMES:
                return False
        except Exception:
            return False

        return True
