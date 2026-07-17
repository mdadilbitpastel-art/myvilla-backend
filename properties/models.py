from django.conf import settings
from django.db import models


class Villa(models.Model):
    """
    A property listed by a host. Populated by the multi-step "Add your Villa"
    wizard on the frontend (Villa Details → Extra Services → Pricing → Payment).
    """

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="villas",
    )

    # --- Villa Details ---
    title = models.CharField(max_length=200)
    property_type = models.CharField(max_length=100, blank=True)  # Villa Living, Hotel…
    city = models.CharField(max_length=120, blank=True)
    country = models.CharField(max_length=120, blank=True)
    address = models.CharField(max_length=255, blank=True)
    description = models.TextField(blank=True)
    build_up_area = models.CharField(max_length=120, blank=True)  # e.g. "2000 Square Yards"
    bedrooms = models.PositiveIntegerField(default=1)
    bathrooms = models.PositiveIntegerField(default=1)
    guests = models.PositiveIntegerField(default=1)

    # --- Facilities / Extra Services --- (free-form list of labels)
    services = models.JSONField(default=list, blank=True)

    # --- Pricing ---
    price_per_night = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    # --- Payment Method ---
    # Which methods guests may pay with (Mastercard, Visa, PayPal, Google Pay).
    accepted_payments = models.JSONField(default=list, blank=True)
    # The host's payout account. payout_account is stored MASKED (last 4 only);
    # full card numbers are never persisted.
    payout_method = models.CharField(max_length=60, blank=True)  # Credit / Debit Card
    payout_account = models.CharField(max_length=64, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "properties_villa"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.title} ({self.owner_id})"

    @property
    def cover_image_url(self) -> str:
        first = self.images.first()
        return first.image.url if first else ""


class Booking(models.Model):
    """
    A guest's reservation of a villa, created from the "Confirm Payment" page.
    Totals are computed and frozen on the server at booking time; card numbers
    are stored MASKED (last 4 only) — the full PAN and CVV are never persisted.
    """

    STATUS_ACTIVE = "active"
    STATUS_CANCELLED = "cancelled"
    STATUS_CHOICES = [
        (STATUS_ACTIVE, "Active"),
        (STATUS_CANCELLED, "Cancelled"),
    ]

    villa = models.ForeignKey(
        Villa, on_delete=models.CASCADE, related_name="bookings"
    )
    guest = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="bookings",
    )

    # --- Trip details ---
    check_in = models.DateField()
    check_out = models.DateField()
    nights = models.PositiveIntegerField(default=1)
    guests = models.PositiveIntegerField(default=1)

    # --- Money (frozen snapshot at booking time) ---
    price_per_night = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    subtotal = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    service_fee = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    # --- Payment (masked) ---
    payment_method = models.CharField(max_length=60, blank=True)
    card_last4 = models.CharField(max_length=24, blank=True)  # "•••• 1234"

    # --- Billing address ---
    billing_street = models.CharField(max_length=255, blank=True)
    billing_apartment = models.CharField(max_length=120, blank=True)
    billing_city = models.CharField(max_length=120, blank=True)
    billing_state = models.CharField(max_length=120, blank=True)
    billing_zip = models.CharField(max_length=32, blank=True)
    billing_country = models.CharField(max_length=120, blank=True)

    # --- Additional information ---
    contact_email = models.EmailField(blank=True)
    contact_phone = models.CharField(max_length=40, blank=True)

    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default=STATUS_ACTIVE
    )
    # Host-side: set when the villa owner responds to the booking (Rent Requests).
    host_responded = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "properties_booking"
        ordering = ["-created_at"]

    def __str__(self):
        return f"Booking #{self.pk} — {self.villa_id} by {self.guest_id}"


class VillaImage(models.Model):
    """
    One image for a villa. The file is saved through Django's configured storage
    backend, so it lands on local disk in dev and on Cloudinary in production —
    the same code path either way.
    """

    villa = models.ForeignKey(Villa, on_delete=models.CASCADE, related_name="images")
    image = models.ImageField(upload_to="villas/")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "properties_villa_image"
        ordering = ["id"]

    def __str__(self):
        return f"Image for villa {self.villa_id}"


class Favorite(models.Model):
    """A villa a user has saved to their wishlist. One row per (user, villa)."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="favorites",
    )
    villa = models.ForeignKey(
        Villa, on_delete=models.CASCADE, related_name="favorited_by"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "properties_favorite"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "villa"], name="uniq_favorite_user_villa"
            )
        ]

    def __str__(self):
        return f"{self.user_id} ♥ villa {self.villa_id}"
