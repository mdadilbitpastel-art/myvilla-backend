from django.db import migrations, models


class Migration(migrations.Migration):
    """
    The headcount the host records when they verify an arrival — how many people
    actually walked in, as against the `guests` the booking was made for.

    Nullable, and left null on every stay already on the books: those check-ins
    happened before the host was ever asked the question, and a 0 (or a copy of
    the booked figure) would be inventing an answer nobody gave.
    """

    dependencies = [
        ("properties", "0029_bookingcancellation"),
    ]

    operations = [
        migrations.AddField(
            model_name="booking",
            name="checked_in_guests",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
    ]
