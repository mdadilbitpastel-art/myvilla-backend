from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from .models import User


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    ordering = ["-date_joined"]
    list_display = ["email", "full_name", "country", "is_staff", "is_active", "date_joined"]
    list_filter = ["is_staff", "is_active", "country"]
    search_fields = ["email", "full_name", "phone_number"]
    readonly_fields = ["date_joined", "updated_at", "last_login"]

    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Profile", {"fields": ("full_name", "phone_number", "country")}),
        ("Permissions", {"fields": ("is_active", "is_staff", "is_superuser", "groups", "user_permissions")}),
        ("Dates", {"fields": ("last_login", "date_joined", "updated_at")}),
    )
    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": ("email", "password1", "password2", "is_staff", "is_superuser"),
            },
        ),
    )
