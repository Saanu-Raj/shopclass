"""
Shared product-import logic, used by both the `import_products` management
command and the web upload endpoint, so there's exactly one place that
understands the Excel column layout.
"""
from django.db import transaction

from classifier.models import Product


class ImportError_(Exception):
    """Raised when the uploaded file doesn't look like a usable product list."""
    pass


REQUIRED_COLUMNS = {"Product Number"}


def import_products_from_workbook(file_obj_or_path, limit=None):
    """
    Reads an Excel product list and creates/updates Product rows.
    Returns (created_count, skipped_count, warnings: list[str]).
    Raises ImportError_ if the file can't be read or is missing required columns.
    """
    # openpyxl is only needed while processing a user-selected workbook; do
    # not load it when Django starts the dashboard/API process.
    import openpyxl

    try:
        wb = openpyxl.load_workbook(file_obj_or_path, read_only=True, data_only=True)
    except Exception as exc:
        raise ImportError_(f"Couldn't read this as an Excel file: {exc}")

    ws = wb.active
    rows = ws.iter_rows(values_only=True)
    try:
        header_row = next(rows)
    except StopIteration:
        raise ImportError_("The file appears to be empty.")

    header = [(h or "").strip() if isinstance(h, str) else h for h in header_row]
    missing = REQUIRED_COLUMNS - set(h for h in header if h)
    if missing:
        raise ImportError_(
            f"Missing required column(s): {', '.join(missing)}. "
            f"Found columns: {', '.join(str(h) for h in header if h)}"
        )

    created, skipped = 0, 0
    warnings = []
    batch = []
    BATCH_SIZE = 500

    def clean(v):
        if isinstance(v, str):
            return v.replace("_x000D_\n", "\n").strip()
        return v or ""

    def flush():
        nonlocal created
        if not batch:
            return
        Product.objects.bulk_create(batch, batch_size=BATCH_SIZE, ignore_conflicts=True)
        created += len(batch)
        batch.clear()

    for i, row in enumerate(rows):
        if limit and i >= limit:
            break
        data = dict(zip(header, row))
        product_number = data.get("Product Number")
        if isinstance(product_number, str):
            product_number = product_number.strip()
        if not product_number:
            skipped += 1
            continue

        image_urls = [data.get(f"Image {n}") for n in range(1, 21)]
        image_urls = [u for u in image_urls if u]

        batch.append(Product(
            product_number=str(product_number),
            model_number=str(data.get("Model Number") or ""),
            source_category=clean(data.get("Product Category")),
            source_sub_category=clean(data.get("Product Sub Category")),
            brand_or_collection=clean(data.get("Collection Name")),
            color=clean(data.get("Product Color") or data.get("Color Collection")),
            name=clean(data.get("Product Name")),
            description=clean(data.get("Product Description ") or data.get("Product Description")),
            bullets=clean(data.get("Bullets")),
            materials=clean(data.get("Materials")),
            image_urls=image_urls,
            raw_row={k: (v if not isinstance(v, str) else clean(v)) for k, v in data.items()},
        ))
        if len(batch) >= BATCH_SIZE:
            with transaction.atomic():
                flush()

    with transaction.atomic():
        flush()

    if created == 0 and skipped == 0:
        warnings.append("No data rows were found below the header.")

    return created, skipped, warnings
