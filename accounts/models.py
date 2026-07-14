from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin
from django.db import models

from .managers import UserManager


class User(AbstractBaseUser, PermissionsMixin):
    """
    Email-based user. Matches the frontend sign-in / register forms:
    email + password (+ optional phone number and country on register).
    """

    email = models.EmailField("email address", unique=True)
    full_name = models.CharField(max_length=150, blank=True)
    phone_number = models.CharField(max_length=32, blank=True)
    country = models.CharField(max_length=100, blank=True)
    # Extra profile-settings fields (free-form strings; UI-formatted).
    gender = models.CharField(max_length=50, blank=True)
    date_of_birth = models.CharField(max_length=100, blank=True)
    address = models.CharField(max_length=255, blank=True)
    emergency_contact = models.CharField(max_length=100, blank=True)
    # Profile picture stored as a small base64 data-URL (client resizes to ≤512px).
    avatar = models.TextField(blank=True, default="")

    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)

    date_joined = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = UserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    class Meta:
        db_table = "accounts_user"
        ordering = ["-date_joined"]

    def __str__(self):
        return self.email
