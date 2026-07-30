from django.contrib import admin

from .models import Booking, CheckInVerification, Coupon, Villa, VillaImage


class VillaImageInline(admin.TabularInline):
    model = VillaImage
    extra = 0


@admin.register(Villa)
class VillaAdmin(admin.ModelAdmin):
    list_display = ("title", "owner", "city", "country", "price_per_night", "created_at")
    list_filter = ("country", "property_type", "created_at")
    search_fields = ("title", "city", "country", "owner__email")
    inlines = [VillaImageInline]


@admin.register(VillaImage)
class VillaImageAdmin(admin.ModelAdmin):
    list_display = ("id", "villa", "created_at")


@admin.register(Booking)
class BookingAdmin(admin.ModelAdmin):
    list_display = (
        "id", "villa", "guest", "check_in", "check_out",
        "nights", "guests", "discount", "coupon_code", "total", "status", "created_at",
    )
    list_filter = ("status", "created_at")
    search_fields = ("villa__title", "guest__email", "contact_email", "coupon_code")


@admin.register(CheckInVerification)
class CheckInVerificationAdmin(admin.ModelAdmin):
    """
    The check-in PIN audit trail. Read-only on purpose: these rows are evidence
    of what happened at a check-in, and editing one would make it worthless.
    The PIN itself is deliberately not listed or searchable — the columns here
    answer "was this guest verified, and did anyone guess at their code", which
    is what an audit actually asks.
    """

    list_display = (
        "id", "booking", "generated_at", "expires_at",
        "failed_attempts", "verified_at", "invalidated_at", "alert_sent_at",
    )
    list_filter = ("verified_at", "alert_sent_at", "generated_at")
    search_fields = ("booking__id", "booking__guest__email", "booking__villa__title")
    readonly_fields = [f.name for f in CheckInVerification._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(Coupon)
class CouponAdmin(admin.ModelAdmin):
    list_display = (
        "code", "owner", "villa", "discount_type", "discount_value",
        "active", "created_at",
    )
    list_filter = ("discount_type", "active", "created_at")
    search_fields = ("code", "owner__email", "villa__title")
