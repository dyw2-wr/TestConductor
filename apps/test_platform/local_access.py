from ipaddress import ip_address

from django.core.exceptions import PermissionDenied


class LocalOnlyMiddleware:
    """Keep the passwordless developer workspace on the local machine."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        remote_address = str(request.META.get("REMOTE_ADDR") or "").strip()
        try:
            is_local = ip_address(remote_address).is_loopback
        except ValueError:
            is_local = False
        if not is_local:
            raise PermissionDenied("TestConductor 仅允许从本机访问")
        return self.get_response(request)
