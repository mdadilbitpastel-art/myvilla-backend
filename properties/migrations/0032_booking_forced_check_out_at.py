from django.db import migrations, models


class Migration(migrations.Migration):
    """
    When the PLATFORM closed a stay rather than the host — half an hour past the
    booked check-out hour, on the clock, with no PIN.

    Nullable and left null everywhere: every departure on the books until now
    was verified by somebody, which is exactly what a null here means.
    """

    dependencies = [
        ("properties", "0031_bookingaddition"),
    ]

    operations = [
        migrations.AddField(
            model_name="booking",
            name="forced_check_out_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
