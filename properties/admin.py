from django.contrib import admin

from .models import Booking, Coupon, Villa, VillaImage


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


@admin.register(Coupon)
class CouponAdmin(admin.ModelAdmin):
    list_display = (
        "code", "owner", "villa", "discount_type", "discount_value",
        "active", "created_at",
    )
    list_filter = ("discount_type", "active", "created_at")
    search_fields = ("code", "owner__email", "villa__title")
