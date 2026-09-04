# Shopify Product Taxonomy Classifier — Prototype

A working prototype that automatically classifies products into the Shopify
Product Taxonomy, detects attributes/values, scores confidence, and gives a
review UI for a human to approve or correct results.

Built with **Python / Django / SQLite** (no paid APIs — everything runs
locally and offline).

## What it does

- Imports a product catalogue from an Excel file uploaded through the dashboard
- Loads the full Shopify Product Taxonomy (14,606 categories, 8,240
  attributes) from the official `Shopify/product-taxonomy` GitHub repo
- Classifies each product using local TF-IDF text matching against the
  taxonomy (title, sub-category, category, materials, description — no
  external API calls, so no cost and no rate limits)
- Detects attribute values (e.g. Color, Material) by scanning the product's
  own fields against each category's allowed attribute values
- Produces a confidence score, up to 5 alternative category suggestions, and
  flags products that need manual review (low confidence, ambiguous top
  matches, missing description/image, or no usable text at all)
- Processes products in batches with a per-product status field, so a
  crashed or interrupted run resumes automatically instead of restarting
- Never lets one bad/broken product (missing image, empty fields, whatever)
  stop the batch — failures are logged per-product and processing continues
- Ships a simple web dashboard + REST API to browse results, filter by
  "needs review", and approve or correct a classification

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

python3 manage.py migrate
python3 manage.py load_taxonomy          # loads categories.json / attributes.json into the DB

python3 manage.py runserver
```

Then open **http://localhost:8000/** for the review dashboard.

**Adding more products:** use the "Add products" bar at the top of the
dashboard to upload another `.xlsx` file (same columns as the sample
product list). It imports the new rows and automatically runs
classification on just the newly-added products — no need to touch the
command line.

Useful flags:
- `import_products <path> --limit 500` — import only the first N rows, for a quick test
- `classify_products --batch-size 500` — tune batch size
- `classify_products --retry-failed` — also retry products that previously errored
- Re-running `classify_products` with no flags only touches products still
  `pending`/`processing` — this is the resume behaviour.

## How classification works (short version)

1. Build a weighted text blob per product: title (×4), sub-category (×5),
   category (×1), materials, and the first 25 words of the description
   (capped, since the marketing copy is long and noisy). The "Collection
   Name" field is deliberately excluded from matching — it's a stylized
   product-line name (e.g. "Lava", "Empress"), not a description, and tends
   to collide with unrelated categories.
2. Compare that text against every **leaf** taxonomy category (the
   categories Shopify actually expects products to be assigned to) using
   TF-IDF + cosine similarity.
3. Take the top match as the prediction, the next few as alternatives.
4. Adjust confidence down if the description or image is missing, and flag
   `ambiguous_top_matches` if the top two scores are within 0.05 of each
   other — that's a genuine sign the system can't tell two categories apart
   and a human should decide.
5. Once a category is picked, scan the product's color/material/name/
   description fields for any of that category's known attribute values.

This is a real, working baseline — not a mock. It's not perfect (see the
sample results below); the written answers explain how a production version
would improve on it (embeddings/LLM-based matching, image classification,
active learning from human corrections, etc).

## API

- `GET /api/products/?needs_review=true` — paginated list, filterable by
  `needs_review`, `approval_status`, `min_confidence`, `max_confidence`, or
  `search=<text>`
- `GET /api/products/<id>/` — full detail incl. classification
- `POST /api/products/<id>/approve/` — accept the current top prediction
- `POST /api/products/<id>/correct/` `{"category_id": <id>}` — human override
- `GET /api/categories/search/?q=<text>` — used by the correction UI to look up categories

## Results on the provided sample (4,999 products)

Ran end-to-end in this environment: **4,999 / 4,999 classified, 0 failures,
~40 seconds total**, 2,896 flagged for manual review (avg confidence 0.48).
That review rate is expected and by design for a keyword/TF-IDF baseline —
see the written answers for how a production system would reduce it.

## Project layout

```
shopclass/
  classifier/
    models.py            # TaxonomyCategory, TaxonomyAttribute, Product, Classification
    engine.py             # the classification logic (TF-IDF matching, attribute detection)
    serializers.py, views.py, urls.py   # REST API + dashboard
    management/commands/
      load_taxonomy.py    # loads categories.json/attributes.json into the DB
      import_products.py  # optional command-line product import
      classify_products.py # resumable batch classifier
    data/
      categories.json, attributes.json  # trimmed Shopify taxonomy (from the official repo)
    templates/classifier/dashboard.html
  shopclass/settings.py, urls.py
```
