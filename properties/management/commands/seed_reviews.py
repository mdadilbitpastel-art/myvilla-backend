"""
Seed demo reviewers + reviews so the reviews section isn't empty on a fresh DB.

Creates a handful of dummy guests (with nice avatar photos) and spreads a few
varied reviews across every existing villa. Idempotent: re-running won't create
duplicate users, and won't add a second demo review by the same guest to a villa
it's already reviewed. These reviews carry no booking (demo only); real guest
reviews are made through the `submitReview` mutation and are tied to a completed
stay.

    python manage.py seed_reviews            # ~3-5 reviews per villa
    python manage.py seed_reviews --per 6    # up to 6 per villa
    python manage.py seed_reviews --clear    # remove demo reviews + users first
"""

import random

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from properties.models import Review, Villa

User = get_user_model()

# Demo reviewers. Avatars are real portrait photos from pravatar.cc (the frontend
# <Avatar> renders any URL directly and falls back to a gender placeholder if one
# ever fails to load). Emails share the @demo.myvilla marker so --clear can find
# them without touching real accounts.
DEMO_DOMAIN = "demo.myvilla"

# A pool of demo reviewers, balanced male/female, each with a real portrait
# photo from pravatar.cc (the `img` number). Enough distinct people (18) so a
# single property can carry 10-12 reviews without repeating a reviewer.
REVIEWERS = [
    ("Aarav Mehta", "Male", 12),
    ("Priya Sharma", "Female", 5),
    ("Rohan Kapoor", "Male", 33),
    ("Ananya Iyer", "Female", 47),
    ("Vikram Nair", "Male", 68),
    ("Sneha Reddy", "Female", 24),
    ("Karan Malhotra", "Male", 51),
    ("Meera Joshi", "Female", 44),
    ("Arjun Desai", "Male", 15),
    ("Isha Verma", "Female", 31),
    ("Daniel Foster", "Male", 60),
    ("Emily Carter", "Female", 9),
    ("Rahul Khanna", "Male", 8),
    ("Nisha Pillai", "Female", 16),
    ("James Bennett", "Male", 52),
    ("Sofia Rossi", "Female", 20),
    ("Aditya Rao", "Male", 3),
    ("Fatima Sheikh", "Female", 49),
]

# Names of common facilities, dropped into a few templates so the reviews read
# like they're about a real stay rather than generic praise.
FEATURES = ["pool", "kitchen", "wifi", "parking", "balcony", "garden", "AC"]

# (rating, comment) templates of varied length/tone. Weighted toward the happy
# end, like a real listing, but with honest 3-4★ notes mixed in. A "{f}" slot
# is filled with a random facility so the review reads specific, not canned.
REVIEW_POOL = [
    (5, "Absolutely stunning property! The photos don't do it justice. Spotlessly clean, the host was super responsive, and the location was perfect for our family getaway. Would book again in a heartbeat."),
    (5, "One of the best stays we've had. Check-in was smooth, the beds were incredibly comfortable, and the view in the morning was unreal. The {f} was a lovely touch."),
    (5, "Perfect little escape. Quiet, private and exactly as described. The {f} made it feel like home and the kitchen had everything we needed."),
    (4, "Really lovely place and great value. Only small thing was the wifi dropped once or twice, but the host sorted it out within the hour."),
    (5, "Hosted a small family celebration here and it was magical. Spacious, well-kept, and the host went above and beyond to make sure we had everything."),
    (4, "Comfortable and clean, close to everything we wanted to see. Would have liked a bit more kitchenware, but overall a really pleasant stay."),
    (5, "Booked it for a long weekend and never wanted to leave. Immaculate rooms, a warm host, and the {f} was a huge bonus for the kids."),
    (5, "Business trip made so much easier by this stay. Fast check-in, quiet at night, strong wifi and a proper workspace. Highly recommend for solo travellers."),
    (3, "Decent stay overall. The place is nice and the host is polite, but it was a little further from the centre than we expected. Fair for the price though."),
    (4, "Beautiful interiors and very peaceful. Check-in was a touch late, but the host apologised and even threw in a late checkout to make up for it."),
    (5, "Can't fault it. Clean, cosy, and the host left us a lovely welcome note and local tips. This is exactly how hosting should be done."),
    (5, "Second time staying here and it's become our go-to. Consistent, spotless, and always a warm welcome. The {f} is our favourite part."),
    (4, "Great for a couples getaway. Romantic setting, comfy bed and good amenities. A few more towels would've been nice, but a small thing."),
    (5, "The whole villa to ourselves felt like a proper luxury. Kids loved the space, we loved the calm. Ten out of ten, will be back next season."),
    (3, "Nice property and an honest listing. Parking was a bit tight on our street, but the stay itself was comfortable and the host was quick to reply."),
    (4, "Solid stay, would recommend. Clean, quiet, and the check-out process was refreshingly simple. The {f} was better than the photos showed."),
    (5, "We travelled with elderly parents and everyone was comfortable. Ground-floor rooms, spotless bathrooms and a host who genuinely cared. Thank you!"),
    (5, "Woke up to birdsong every morning. The {f} was spotless and the whole place had a calm, homely feel. Already recommended it to two friends."),
    (4, "Good location, easy self check-in, and very clean. It got a little warm in the afternoon but the AC handled it fine. Would stay again."),
    (5, "Exceptional value for what you get. Big rooms, fast wifi, and the host shared great food recommendations nearby. Couldn't have asked for more."),
    (5, "A proper home away from home. Everything worked, everything was clean, and the {f} made the evenings special. Five stars, no hesitation."),
    (4, "Lovely stay for our anniversary. Peaceful and private. Only note is the road in is a bit narrow, but nothing that spoiled the trip."),
]


class Command(BaseCommand):
    help = "Seed demo reviewers and reviews across all villas."

    def add_arguments(self, parser):
        parser.add_argument("--per", type=int, default=12, help="Max reviews per villa.")
        parser.add_argument(
            "--clear",
            action="store_true",
            help="Delete demo reviews and demo users, then reseed.",
        )

    def handle(self, *args, **opts):
        rng = random.Random(42)  # stable across runs
        per = max(1, min(opts["per"], len(REVIEWERS)))

        if opts["clear"]:
            demo_users = User.objects.filter(email__endswith=f"@{DEMO_DOMAIN}")
            n_rev = Review.objects.filter(guest__in=demo_users).count()
            Review.objects.filter(guest__in=demo_users).delete()
            n_usr = demo_users.count()
            demo_users.delete()
            self.stdout.write(f"Cleared {n_rev} demo reviews and {n_usr} demo users.")

        # Ensure the demo reviewers exist.
        users = []
        for name, gender, img in REVIEWERS:
            slug = name.lower().replace(" ", ".")
            email = f"{slug}@{DEMO_DOMAIN}"
            user, created = User.objects.get_or_create(
                email=email,
                defaults={
                    "full_name": name,
                    "gender": gender,
                    "avatar": f"https://i.pravatar.cc/300?img={img}",
                },
            )
            if created:
                user.set_unusable_password()
                user.save(update_fields=["password"])
            users.append(user)

        villas = list(Villa.objects.all())
        if not villas:
            self.stdout.write(self.style.WARNING("No villas to review yet."))
            return

        made = 0
        for villa in villas:
            already = set(
                Review.objects.filter(villa=villa, booking__isnull=True).values_list(
                    "guest_id", flat=True
                )
            )
            # A different-but-stable number of reviews per villa (10-12 by
            # default), capped by how many fresh reviewers remain.
            want = rng.randint(max(1, per - 2), per)
            pool = [u for u in users if u.id not in already]
            rng.shuffle(pool)
            for user in pool[:want]:
                rating, comment = rng.choice(REVIEW_POOL)
                # Fill the "{f}" facility slot so the text reads specific.
                comment = comment.replace("{f}", rng.choice(FEATURES))
                Review.objects.create(
                    villa=villa,
                    guest=user,
                    booking=None,
                    rating=rating,
                    comment=comment,
                )
                made += 1

        avg_note = "" if not made else " (varied ratings + avatars)"
        self.stdout.write(
            self.style.SUCCESS(
                f"Seeded {made} demo reviews across {len(villas)} villas{avg_note}."
            )
        )
