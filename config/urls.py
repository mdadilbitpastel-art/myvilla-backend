from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.http import JsonResponse
from django.urls import path
from django.views.decorators.csrf import csrf_exempt
from strawberry.django.views import GraphQLView

from config.schema import schema


def health(_request):
    return JsonResponse({"status": "ok"})


urlpatterns = [
    path("admin/", admin.site.urls),
    # JWT API is stateless, so the GraphQL endpoint is CSRF-exempt.
    path("graphql/", csrf_exempt(GraphQLView.as_view(schema=schema))),
    path("health/", health),
]

# In local dev (no Cloudinary), Django serves uploaded villa images from disk.
# In production images live on Cloudinary's CDN, so this is only needed here.
if settings.DEBUG and not settings.CLOUDINARY_URL:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
