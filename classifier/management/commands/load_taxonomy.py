import json
from pathlib import Path

from django.core.management.base import BaseCommand
from django.db import transaction

from classifier.models import TaxonomyAttribute, TaxonomyCategory

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"


class Command(BaseCommand):
    help = "Loads the Shopify product taxonomy (categories + attributes) into the database."

    def handle(self, *args, **options):
        self.load_attributes()
        self.load_categories()
        self.stdout.write(self.style.SUCCESS("Taxonomy loaded."))

    def load_attributes(self):
        path = DATA_DIR / "attributes.json"
        attrs = json.loads(path.read_text())
        self.stdout.write(f"Loading {len(attrs)} attributes...")
        objs = [
            TaxonomyAttribute(gid=a["id"], name=a["name"], handle=a["handle"], values=a["values"])
            for a in attrs
        ]
        with transaction.atomic():
            TaxonomyAttribute.objects.all().delete()
            TaxonomyAttribute.objects.bulk_create(objs, batch_size=1000)

    def load_categories(self):
        path = DATA_DIR / "categories.json"
        cats = json.loads(path.read_text())
        self.stdout.write(f"Loading {len(cats)} categories...")

        attr_map = {a.gid: a.id for a in TaxonomyAttribute.objects.all()}

        with transaction.atomic():
            TaxonomyCategory.objects.all().delete()
            # First pass: create all categories without parent links (avoid ordering issues).
            objs = []
            for c in cats:
                search_text = f"{c['name']} {c['full_name']}".lower()
                objs.append(TaxonomyCategory(
                    gid=c["id"],
                    level=c["level"],
                    name=c["name"],
                    full_name=c["full_name"],
                    parent=None,
                    search_text=search_text,
                ))
            TaxonomyCategory.objects.bulk_create(objs, batch_size=1000)

            # Second pass: set parents.
            gid_to_pk = dict(TaxonomyCategory.objects.values_list("gid", "id"))
            to_update = []
            cat_by_gid = {c["id"]: c for c in cats}
            for cat in TaxonomyCategory.objects.all().iterator():
                src = cat_by_gid[cat.gid]
                if src["parent_id"] and src["parent_id"] in gid_to_pk:
                    cat.parent_id = gid_to_pk[src["parent_id"]]
                    to_update.append(cat)
            TaxonomyCategory.objects.bulk_update(to_update, ["parent_id"], batch_size=1000)

            # Third pass: link attributes (M2M).
            through_model = TaxonomyCategory.attributes.through
            through_objs = []
            for c in cats:
                cat_pk = gid_to_pk.get(c["id"])
                if not cat_pk:
                    continue
                for attr_gid in c["attribute_ids"]:
                    attr_pk = attr_map.get(attr_gid)
                    if attr_pk:
                        through_objs.append(through_model(
                            taxonomycategory_id=cat_pk, taxonomyattribute_id=attr_pk
                        ))
            through_model.objects.bulk_create(through_objs, batch_size=5000, ignore_conflicts=True)
