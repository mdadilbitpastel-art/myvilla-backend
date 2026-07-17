"""Create this project's Postgres schema before migrations run.

We share one physical Postgres database across several projects and keep each
project's tables in its own schema (see DB_SCHEMA in settings). The search_path
points at that schema, but Postgres won't create it automatically, so `migrate`
would fail on a fresh schema. This command creates it up front and is idempotent.
"""

import os

from django.core.management.base import BaseCommand
from django.db import connection


class Command(BaseCommand):
    help = "Create the Postgres schema named in DB_SCHEMA if it does not exist."

    def handle(self, *args, **options):
        schema = os.environ.get("DB_SCHEMA", "").strip()
        if not schema:
            self.stdout.write("DB_SCHEMA not set — skipping schema creation.")
            return
        if connection.vendor != "postgresql":
            self.stdout.write(f"Vendor is {connection.vendor!r}, not Postgres — skipping.")
            return
        # CREATE SCHEMA IF NOT EXISTS resolves regardless of search_path, so it
        # works even when the target schema doesn't exist yet.
        with connection.cursor() as cursor:
            cursor.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
        self.stdout.write(self.style.SUCCESS(f"Schema {schema!r} is ready."))
