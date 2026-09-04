from django.core.management import call_command
from django.core.management.base import BaseCommand

from classifier.models import TaxonomyCategory


class Command(BaseCommand):
    """Load taxonomy data for a new database without touching product data."""

    help = "Loads taxonomy data only when the database has no taxonomy categories."

    def handle(self, *args, **options):
        if TaxonomyCategory.objects.exists():
            self.stdout.write("Taxonomy already present; leaving existing data unchanged.")
            return

        call_command("load_taxonomy")
