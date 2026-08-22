"""Zero-dependency CORS middleware for the demo API.

The operator console (Vite dev server) and the agent service run on other
origins; this opens the API to them without pulling in django-cors-headers.
"""

from django.http import HttpResponse

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, PUT, PATCH, DELETE, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Authorization, X-Session-ID",
    "Access-Control-Max-Age": "86400",
}


class CorsMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.method == "OPTIONS":
            response = HttpResponse(status=204)
        else:
            response = self.get_response(request)
        for header, value in CORS_HEADERS.items():
            response[header] = value
        return response
