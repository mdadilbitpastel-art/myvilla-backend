"""
Decode a base64 data-URL into a Django file, so uploaded images can flow
through the configured storage backend (local disk in dev, Cloudinary in prod).
"""

import base64
import binascii
import uuid

from django.core.files.base import ContentFile
from graphql import GraphQLError

# Keep individual images modest — the client already downscales before sending.
MAX_IMAGE_BYTES = 6 * 1024 * 1024  # 6 MB decoded

_EXT_BY_MIME = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
}


def data_url_to_file(data_url: str) -> ContentFile:
    """
    Convert a `data:image/...;base64,...` string into a named ContentFile.
    Raises GraphQLError on anything that isn't a valid, supported image.
    """
    data_url = (data_url or "").strip()
    if not data_url.startswith("data:image/") or ";base64," not in data_url:
        raise GraphQLError("Each image must be a valid base64 image.")

    header, _, payload = data_url.partition(";base64,")
    mime = header[len("data:"):].lower()
    ext = _EXT_BY_MIME.get(mime)
    if ext is None:
        raise GraphQLError("Unsupported image type. Use JPG, PNG, WebP or GIF.")

    try:
        raw = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError):
        raise GraphQLError("An image could not be read.")

    if not raw:
        raise GraphQLError("An image was empty.")
    if len(raw) > MAX_IMAGE_BYTES:
        raise GraphQLError("An image is too large. Please choose a smaller one.")

    return ContentFile(raw, name=f"{uuid.uuid4().hex}.{ext}")
