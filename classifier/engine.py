"""
Classification engine: matches a product against the Shopify taxonomy using
local TF-IDF text similarity (no external API calls -- free and offline).

Design:
- We only match against LEAF categories (categories with no children), since
  those are the ones Shopify actually expects products to be assigned to.
- The vectorizer is fit once on all leaf categories' text and cached in
  memory for the lifetime of the process (see get_engine()).
- For each product we build a text blob from title, description, bullets,
  source category/sub-category, and brand -- weighted so the title and
  source category matter more than the long description.
- Attribute values are then guessed by scanning the product's own fields
  (color, materials, name, description) for any of the attribute's known
  values, restricted to the attributes that category actually defines.
"""
import re
from dataclasses import dataclass, field
from typing import Optional

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from classifier.models import TaxonomyCategory

LOW_CONFIDENCE_THRESHOLD = 0.35
TOP_N_ALTERNATIVES = 5


def _clean_text(text: str) -> str:
    text = text or ""
    text = re.sub(r"<[^>]+>", " ", text)  # strip any stray HTML
    text = re.sub(r"[^a-zA-Z0-9\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip().lower()


@dataclass
class CategoryMatch:
    category_id: int
    gid: str
    full_name: str
    score: float


@dataclass
class ClassificationResult:
    best: Optional[CategoryMatch]
    alternatives: list = field(default_factory=list)
    review_reasons: list = field(default_factory=list)

    @property
    def needs_review(self) -> bool:
        return bool(self.review_reasons)


class TaxonomyEngine:
    """Holds the fitted TF-IDF matrix for all leaf categories."""

    def __init__(self):
        self.vectorizer = None
        self.matrix = None
        self.categories = []  # list of TaxonomyCategory, aligned with matrix rows
        self._fit()

    def _fit(self):
        qs = (
            TaxonomyCategory.objects.filter(children__isnull=True)
            .only("id", "gid", "full_name", "search_text")
        )
        self.categories = list(qs)
        corpus = [_clean_text(c.search_text) for c in self.categories]
        self.vectorizer = TfidfVectorizer(
            ngram_range=(1, 1), stop_words="english", sublinear_tf=True, min_df=1
        )
        self.matrix = self.vectorizer.fit_transform(corpus)

    def classify_text(self, text: str, top_n: int = TOP_N_ALTERNATIVES) -> list[CategoryMatch]:
        clean = _clean_text(text)
        if not clean:
            return []
        vec = self.vectorizer.transform([clean])
        sims = cosine_similarity(vec, self.matrix)[0]
        top_idx = sims.argsort()[::-1][:top_n]
        return [
            CategoryMatch(
                category_id=self.categories[i].id,
                gid=self.categories[i].gid,
                full_name=self.categories[i].full_name,
                score=float(sims[i]),
            )
            for i in top_idx if sims[i] > 0
        ]


_engine_instance: Optional[TaxonomyEngine] = None


def get_engine() -> TaxonomyEngine:
    global _engine_instance
    if _engine_instance is None:
        _engine_instance = TaxonomyEngine()
    return _engine_instance


def build_product_text(product) -> tuple[str, dict]:
    """Builds the weighted text blob used for matching, and reports which
    signal fields were actually available (used for confidence adjustment)."""
    signals = {
        "has_title": bool(product.name),
        "has_description": bool(product.description),
        "has_category_hint": bool(product.source_category or product.source_sub_category),
        "has_image": bool(product.image_urls),
        "has_brand": bool(product.brand_or_collection),
    }
    # Repeat the more reliable/short fields to weight them higher in TF-IDF.
    # Marketing description/bullets are noisy (flowery copywriting), so they're
    # included but capped and weighted low -- they help fill gaps, not dominate.
    # Note: "Collection Name" (e.g. "Lava", "Empress") is a stylized product
    # line name, not a descriptive brand -- it's stored and shown for review
    # but deliberately left OUT of the matching text since it's often a real
    # English word that coincidentally collides with unrelated categories
    # (e.g. "Lava" the collection vs. "Lava Lamps" the category).
    parts = []
    if product.name:
        parts += [product.name] * 4
    if product.source_sub_category:
        parts += [product.source_sub_category] * 5
    if product.source_category:
        parts += [product.source_category] * 1
    if product.materials:
        parts.append(product.materials)
    if product.description:
        # Cap to the first ~25 words: enough for topical signal, not enough
        # for flowery copywriting to drown out the title/category fields.
        capped = " ".join(product.description.split()[:25])
        parts.append(capped)
    return " ".join(parts), signals


def classify_product(product) -> ClassificationResult:
    engine = get_engine()
    text, signals = build_product_text(product)
    review_reasons = []

    if not text.strip():
        review_reasons.append("no_usable_text")
        return ClassificationResult(best=None, alternatives=[], review_reasons=review_reasons)

    matches = engine.classify_text(text)
    if not matches:
        review_reasons.append("no_matching_category")
        return ClassificationResult(best=None, alternatives=[], review_reasons=review_reasons)

    best = matches[0]
    alternatives = matches[1:]

    # Confidence adjustments based on data completeness (documented in Q6/Q7 answers).
    confidence = best.score
    if not signals["has_description"]:
        confidence *= 0.85
        review_reasons.append("missing_description")
    if not signals["has_image"]:
        confidence *= 0.95
        review_reasons.append("missing_image")
    confidence = min(confidence, 1.0)
    best.score = confidence

    if confidence < LOW_CONFIDENCE_THRESHOLD:
        review_reasons.append("low_confidence")

    # If the top two matches are very close, that's genuine category ambiguity.
    if len(matches) > 1 and (matches[0].score - matches[1].score) < 0.05:
        review_reasons.append("ambiguous_top_matches")

    return ClassificationResult(best=best, alternatives=alternatives, review_reasons=review_reasons)


def detect_attributes(product, category: TaxonomyCategory) -> dict:
    """For each attribute defined on the category, look for its known values
    inside the product's own fields (color, materials, name, description)."""
    haystack_fields = {
        "color": product.color,
        "materials": product.materials,
        "name": product.name,
        "description": product.description,
        "bullets": product.bullets,
    }
    combined_haystack = _clean_text(" ".join(v for v in haystack_fields.values() if v))

    detected = {}
    for attr in category.attributes.all():
        found_value = None
        found_field = None
        for value in attr.values:
            value_clean = _clean_text(value)
            if not value_clean:
                continue
            # Prefer an exact field match (e.g. product.color == "Blue") first.
            if value_clean == _clean_text(product.color):
                found_value, found_field = value, "color"
                break
            if re.search(rf"\b{re.escape(value_clean)}\b", combined_haystack):
                found_value, found_field = value, "text_match"
        if found_value:
            detected[attr.name] = {
                "value": found_value,
                "source": found_field,
                "confidence": 0.9 if found_field == "color" else 0.6,
            }
    return detected
