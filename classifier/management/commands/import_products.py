from django.core.management.base import BaseCommand, CommandError

from classifier.importers import import_products_from_workbook, ImportError_


class Command(BaseCommand):
    help = "Imports products from an Excel product list into the database."

    def add_arguments(self, parser):
        parser.add_argument("xlsx_path", type=str, help="Path to the Product List .xlsx file")
        parser.add_argument("--limit", type=int, default=None, help="Only import the first N rows (for testing)")

    def handle(self, *args, **options):
        try:
            created, skipped, warnings = import_products_from_workbook(
                options["xlsx_path"], limit=options["limit"]
            )
        except ImportError_ as exc:
            raise CommandError(str(exc))

        for w in warnings:
            self.stdout.write(self.style.WARNING(w))
        self.stdout.write(self.style.SUCCESS(
            f"Import complete. Created/updated: {created}, skipped (no product number): {skipped}"
        ))
