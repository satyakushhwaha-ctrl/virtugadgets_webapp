"""Deterministic matching of Amazon and Flipkart staging products."""

from dataclasses import dataclass
import re
import unicodedata
from urllib.parse import urlparse

from django.db import transaction

from ..models import (
    AmazonProduct,
    FlipkartProduct,
    ImportStatus,
    ImportBatch,
    MatchConfidence,
    MatchStatus,
    ProductMatch,
)


WEIGHTS = {
    "brand": 20,
    "model": 35,
    "storage": 15,
    "ram": 10,
    "color": 5,
    "processor": 5,
    "display": 5,
    "other_specs": 5,
}


@dataclass(frozen=True)
class KeywordMatchingSummary:
    amazon_products: int
    flipkart_products: int
    matches_created: int
    matches_updated: int
    high_confidence: int
    medium_confidence: int
    low_confidence: int
    no_candidate: int
    failed: int

CAPACITY_PATTERN = re.compile(
    r"(?P<amount>\d+(?:\.\d+)?)\s*(?P<unit>gb|tb)"
    r"(?:\s*(?:ram|rom|storage))?",
    re.IGNORECASE,
)

TITLE_STOPWORDS = {
    "and", "for", "with", "the", "a", "an", "in", "on", "of", "by",
    "new", "latest", "home", "edition", "core", "inch", "inches", "cm",
    "laptop", "computer", "notebook", "gaming", "mobile", "phone",
}

SEARCH_MATCH_WEIGHTS = {
    "model": 35,
    "family": 5,
    "cpu": 15,
    "gpu": 15,
    "gpu_memory": 5,
    "ram": 8,
    "storage": 10,
    "color": 5,
    "display": 5,
    "brand": 10,
    "os": 3,
    "title": 5,
}

MODEL_MODIFIERS = {
    "ultra", "edge", "fe", "plus", "pro", "max", "mini", "se", "air",
    "fold", "flip",
}

ACCESSORY_PATTERNS = (
    r"\bback\s*cover\b", r"\bflip\s*cover\b", r"\bphone\s*case\b",
    r"\bcase\b", r"\bcover\b", r"\bscreen\s*(?:guard|protector)\b",
    r"\btempered\s+glass\b", r"\b(?:camera|lens)\s*protector\b",
    r"\bglass\s+protector\b", r"\bprotector\b", r"\bcharger\b",
    r"\bcharging\s+cable\b", r"\busb\s+cable\b", r"\badapter\b",
    r"\breplacement\b", r"\b(?:phone|mobile)\s+holder\b", r"\bskin\b",
    r"\bsleeve\b", r"\bpouch\b", r"\bstrap\b", r"\bstand\b",
    r"\b(?:mount|keyboard|mouse|stylus)\b", r"\bpen\s+replacement\b",
)


def classify_product_type(value) -> str:
    """Classify a title/record for compatibility without changing the schema."""
    text = _combined_search_text(value).casefold()
    if any(re.search(pattern, text) for pattern in ACCESSORY_PATTERNS):
        return "accessory"
    if re.search(r"\b(?:smartphone|mobile\s+phone|iphone|galaxy\s+s\d+|pixel)\b", text):
        return "smartphone"
    if re.search(r"\b(?:laptop|notebook|macbook)\b", text):
        return "laptop"
    if re.search(r"\b(?:tablet|ipad)\b", text):
        return "tablet"
    if re.search(r"\b(?:television|tv|qled|oled)\b", text):
        return "tv"
    if re.search(r"\b(?:headphone|earphone|earbud|airpods)\b", text):
        return "audio"
    return "unknown"


def _phone_model_signals(text: str) -> tuple[str, str, str]:
    """Return normalized Samsung-style family, modifier, and identity."""
    match = re.search(
        r"\b(?:galaxy\s+)?s\s*(?P<number>\d{2})"
        r"(?:\s*(?P<modifier>ultra|edge|fe|plus|pro|max|mini|se|air|fold|flip))?\b",
        text,
        re.IGNORECASE,
    )
    if not match:
        return "", "", ""
    family = f"s{match.group('number')}"
    modifier = (match.group("modifier") or "").casefold()
    return family, modifier, f"{family} {modifier}".strip()


def _color_signals(value, text: str) -> set[str]:
    field = normalize_text(getattr(value, "color", "")) if not isinstance(value, str) else ""
    colors = {
        "titanium gray", "titanium black", "titanium silverblue", "titanium silver",
        "black", "white", "blue", "gray", "grey", "silver", "gold", "green",
    }
    found = {color for color in colors if color in text}
    if field:
        found.add(field)
    return found

GENERIC_IDENTITY_WORDS = {
    "amd", "basic", "computer", "core", "ddr", "edition", "gaming",
    "graphics", "hexa", "home", "inch", "inches", "laptop", "mobile",
    "nvidia", "notebook", "office", "processor", "ram", "ssd", "thin",
    "with", "windows", "work", "xbox", "upgradeable",
}


def _combined_search_text(value) -> str:
    """Combine title and available structured fields for search candidates."""
    if isinstance(value, str):
        return value
    return " ".join(
        str(getattr(value, field, "") or "")
        for field in (
            "product_title", "title", "brand", "processor", "ram", "storage",
            "operating_system", "display_size", "resolution",
        )
    )


def _identity_signals(value) -> dict[str, object]:
    """Extract marketplace-independent product identity signals.

    Search result pages generally expose only a title, so these signals are
    deliberately conservative and deterministic. Missing data is neutral;
    contradictory data is a real negative signal.
    """
    raw = _combined_search_text(value)
    text = normalize_text(raw)
    tokens = set(normalized_title_tokens(text))
    model_family, model_variant, phone_model = _phone_model_signals(text)

    brand = normalize_text(getattr(value, "brand", "")) if not isinstance(value, str) else ""
    if not brand:
        brand = next((token for token in tokens if token in {
            "acer", "apple", "asus", "dell", "hp", "lenovo", "msi", "samsung",
            "oneplus", "realme", "xiaomi", "vivo", "infinix", "motorola",
        }), "")

    cpu = set()
    for match in re.finditer(
        r"\bryzen\s*(?P<tier>[3579])\s*(?:hexa\s*core\s*)?"
        r"(?P<model>\d{4,5}[a-z]{0,3})\b",
        text,
    ):
        cpu.add(f"ryzen{match.group('tier')} {match.group('model')}")
    for match in re.finditer(
        r"\b(?:intel\s+)?core\s*(?P<tier>i?[3579]|[3579])\s*"
        r"(?:\d+(?:th|nd|rd|st)\s+gen\s*)?"
        r"(?P<model>\d{3,5}[a-z]{0,3})\b",
        text,
    ):
        cpu.add(f"core{match.group('tier').replace('i', 'i')} {match.group('model')}")
    if not cpu:
        for match in re.finditer(r"\bryzen\s*(?:ai\s*)?(?P<tier>[3579])\b", text):
            cpu.add(f"ryzen{match.group('tier')}")
        for match in re.finditer(r"\b(?:intel\s+)?core\s*(?P<tier>i?[3579]|[3579])\b", text):
            cpu.add(f"core{match.group('tier')}")

    gpu = {
        re.sub(r"\s+", "", match.group(0))
        for match in re.finditer(r"\b(?:rtx|gtx|rx|arc)\s*\d{3,4}\b", text)
    }
    gpu_memory = set()
    for match in re.finditer(r"(?P<memory>\d+)\s*gb\s*graphics\b", text):
        gpu_memory.add(f"{match.group('memory')}gb")
    for match in re.finditer(
        r"(?P<memory>\d+)\s*gb\s*(?:graphics|vram)?\s*"
        r"(?:nvidia\s+)?(?:geforce\s+)?(?:rtx|gtx|rx|arc)\b",
        text,
    ):
        gpu_memory.add(f"{match.group('memory')}gb")
    for match in re.finditer(
        r"(?:rtx|gtx|rx|arc)\s*\d{3,4}[^,;()]{0,25}?"
        r"(?P<memory>\d+)\s*gb\s*(?:graphics|vram)",
        text,
    ):
        gpu_memory.add(f"{match.group('memory')}gb")

    storage = set()
    for match in re.finditer(r"(?P<size>\d+(?:\.\d+)?)\s*(?P<unit>gb|tb)\s*"
                             r"(?P<kind>nvme\s+)?(?:ssd|hdd|hard\s+drive)", text):
        size = match.group("size")
        if size.endswith(".0"):
            size = size[:-2]
        # Capacity is the stable cross-marketplace identity. SSD/NVMe/HDD
        # wording is retained by the product extractor, but a search title
        # that omits the medium must not conflict with the same capacity.
        storage.add(f"{size}{match.group('unit')}")
    if not storage:
        field_storage = normalize_text(getattr(value, "storage", "")) if not isinstance(value, str) else ""
        storage.update(
            value.replace(" ", "")
            for value in re.findall(r"\d+(?:\.\d+)?\s*(?:gb|tb)", field_storage)
        )

    capacities = {
        f"{match.group('amount')}{match.group('unit')}"
        for match in CAPACITY_PATTERN.finditer(text)
    }
    if not storage and capacities:
        field_ram_text = normalize_text(getattr(value, "ram", "")) if not isinstance(value, str) else ""
        explicit_ram = set(re.findall(r"\d+(?:\.\d+)?\s*(?:gb|tb)", field_ram_text))
        storage_candidates = capacities - explicit_ram - gpu_memory
        if storage_candidates:
            storage.add(max(
                storage_candidates,
                key=lambda item: float(re.match(r"\d+(?:\.\d+)?", item).group()),
            ))
    ram = set()
    for capacity in capacities:
        if capacity not in gpu_memory and not any(
            capacity.startswith(storage_value.split()[0]) for storage_value in storage
        ):
            ram.add(capacity)
    field_ram = normalize_text(getattr(value, "ram", "")) if not isinstance(value, str) else ""
    ram.update(
        value.replace(" ", "")
        for value in re.findall(r"\d+(?:\.\d+)?\s*(?:gb|tb)", field_ram)
    )
    # A graphics-memory token is not system RAM.
    ram -= gpu_memory

    os = set()
    for match in re.finditer(r"\b(?:windows|win)\s*([0-9]{1,2})", text):
        os.add(f"windows {match.group(1)}")
    os.update(
        f"{match.group(1)} {match.group(2)}"
        for match in re.finditer(r"\b(windows)\s*(\d{1,2})", text)
    )

    model = set()
    technical_prefixes = ("ryzen", "core", "rtx", "gtx", "radeon", "arc", "windows")
    for token in tokens:
        compact = token.replace("_", "")
        if (
            compact in {"m365", "windows11"}
            or compact in cpu
            or compact in gpu
            or compact.startswith(technical_prefixes + ("win", "ddr"))
        ):
            continue
        if re.fullmatch(r"[a-z]{2,}[0-9][a-z0-9-]*", compact):
            # Marketplace display variants commonly prefix the same SKU with
            # a screen size, e.g. 15-fb3130AX.
            model.add(re.sub(r"^\d+-", "", compact))
    if phone_model:
        model = {phone_model}

    family_tokens = {
        token for token in tokens
        if len(token) > 2
        and token not in GENERIC_IDENTITY_WORDS
        and token not in brand.split()
        and not re.fullmatch(r"\d+(?:gb|tb|hz|kg|cm)", token)
        and not re.fullmatch(r"\d+(?:th|nd|rd|st)", token)
        and token not in {item.replace(" ", "") for item in cpu | gpu | gpu_memory | ram}
        and token not in {"windows11", "windows", "win11", "ddr5", "ddr4"}
    }
    if model:
        family_tokens -= model

    display = set(re.findall(r"\d+(?:\.\d+)?\s*(?:inch|inches|cm)", text))
    return {
        "tokens": tokens,
        "brand": {brand} if brand else set(),
        "family": family_tokens,
        "model": model,
        "model_family": {model_family} if model_family else set(),
        "model_variant": {model_variant} if model_variant else set(),
        "cpu": cpu,
        "gpu": gpu,
        "gpu_memory": gpu_memory,
        "ram": ram,
        "storage": storage,
        "os": os,
        "display": display,
        "color": _color_signals(value, text),
        "truncated": bool(re.search(r"(?:\.\.\.|…)\s*$", raw)),
    }


def normalize_text(value: str | None) -> str:
    """Normalize case, punctuation, whitespace, and Unicode variants."""
    value = unicodedata.normalize("NFKC", str(value or "")).casefold()
    value = re.sub(r"[^\w]+", " ", value, flags=re.UNICODE)
    return " ".join(value.split())


def normalized_title_tokens(value: str | None) -> list[str]:
    """Return safe title tokens while retaining model and capacity identifiers."""
    normalized = normalize_text(value)
    normalized = re.sub(r"(?<=\d)\s+(?=(?:gb|tb)\b)", "", normalized)
    raw_tokens = re.findall(r"[a-z]+\d+[a-z0-9]*|\d+[a-z]+[a-z0-9]*|[a-z]+|\d+", normalized)
    tokens = []
    index = 0
    while index < len(raw_tokens):
        if (
            index + 1 < len(raw_tokens)
            and raw_tokens[index].isalpha()
            and raw_tokens[index + 1].isdigit()
            and raw_tokens[index] in {
                "ryzen", "core", "iphone", "windows", "android", "ios",
                "macos", "rtx", "gtx", "radeon", "arc",
            }
        ):
            tokens.append(raw_tokens[index] + raw_tokens[index + 1])
            index += 2
            continue
        tokens.append(raw_tokens[index])
        index += 1
    return tokens


def _title_signal_tokens(value: str | None) -> set[str]:
    return {
        token for token in normalized_title_tokens(value)
        if token not in TITLE_STOPWORDS and len(token) > 1
    }


def _search_match_text(product_or_title) -> str:
    if isinstance(product_or_title, str):
        return product_or_title
    fields = [
        getattr(product_or_title, "product_title", ""),
        getattr(product_or_title, "brand", ""),
        getattr(product_or_title, "processor", ""),
        getattr(product_or_title, "ram", ""),
        getattr(product_or_title, "storage", ""),
        getattr(product_or_title, "operating_system", ""),
        getattr(product_or_title, "display_size", ""),
        getattr(product_or_title, "resolution", ""),
    ]
    return " ".join(str(field or "") for field in fields)


def _search_match_signals(value: str | None) -> dict[str, set[str]]:
    """Extract comparable technical signals from a title or product fields."""
    raw_text = str(value or "")
    truncated = bool(re.search(r"(?:\.\.\.|…)[\s]*$", raw_text))
    text = normalize_text(raw_text)
    tokens = _title_signal_tokens(text)
    capacities = {
        token for token in tokens if re.fullmatch(r"\d+(?:gb|tb)", token)
    }
    cpu = set()
    for pattern in (
        r"ryzen\s*\d+",
        r"core\s*i[3579](?:\s*[- ]?\s*\d+)?",
        r"\bi[3579]\s*[- ]?\s*\d+",
        r"\b\d{4,6}[a-z]{1,4}\b",
    ):
        cpu.update(re.sub(r"\s+", "", match) for match in re.findall(pattern, text))
    gpu = {
        re.sub(r"\s+", "", match)
        for match in re.findall(r"\b(?:rtx|gtx|rx|arc)\s*\d{3,4}\b", text)
    }
    gpu_memory = set()
    for match in re.finditer(
        r"(?P<memory>\d+)\s*(?P<unit>gb|tb)\s*graphics\b",
        text,
    ):
        gpu_memory.add(f"{match.group('memory')}{match.group('unit')}")
    for match in re.finditer(
        r"(?P<memory>\d+)\s*(?P<unit>gb|tb)\s*(?:graphics\s*)?(?:nvidia\s+)?(?:geforce\s+)?(?:rtx|gtx|rx|arc)\s*\d{3,4}",
        text,
    ):
        gpu_memory.add(f"{match.group('memory')}{match.group('unit')}")
    for match in re.finditer(
        r"(?:rtx|gtx|rx|arc)\s*\d{3,4}[^,;()]{0,30}?(?P<memory>\d+)\s*(?P<unit>gb|tb)\s*(?:graphics|vram)",
        text,
    ):
        gpu_memory.add(f"{match.group('memory')}{match.group('unit')}")
    os = {
        re.sub(r"\s+", "", match)
        for match in re.findall(r"\b(?:windows|android|ios|macos)\s*\d*\b", text)
    }
    display = {
        re.sub(r"\s+", "", match)
        for match in re.findall(
            r"\b\d+(?:\.\d+)?\s*(?:inch|inches|cm)\b",
            raw_text.casefold(),
        )
    }

    technical = capacities | cpu | gpu | os
    def is_model_identifier(token: str) -> bool:
        if token in technical or token in {"ddr", "ssd"}:
            return False
        if token.startswith(("ips", "nits")) or token.endswith(("hz", "kg", "cm")):
            return False
        if not any(character.isalpha() for character in token):
            return False
        if not any(character.isdigit() for character in token):
            return False
        numeric_runs = re.findall(r"\d+", token)
        return max((len(run) for run in numeric_runs), default=0) >= 3 or token.startswith("iphone")

    model = {token for token in tokens if is_model_identifier(token)}
    brand = tokens & _title_signal_tokens(getattr(value, "brand", "")) if not isinstance(value, str) else set()
    generic = tokens - technical - model - brand
    return {
        "tokens": tokens,
        "model": model,
        "cpu": cpu,
        "gpu": gpu,
        "gpu_memory": gpu_memory,
        "capacities": capacities,
        "os": os,
        "display": display,
        "brand": brand,
        "generic": generic,
        "truncated": truncated,
    }


def rank_flipkart_search_result(amazon_product, search_result) -> dict:
    """Score one unextracted Flipkart search result against an Amazon title.

    This is deliberately title-only: search candidates have no structured
    Flipkart attributes yet. The weights mirror the existing matcher’s intent,
    with identifier/spec tokens receiving most of the score.
    """
    amazon_signals = _identity_signals(amazon_product)
    candidate_signals = _identity_signals(search_result.title)
    matched = {}
    component_scores = {}
    conflicts = {}
    source_product_type = classify_product_type(amazon_product)
    candidate_product_type = classify_product_type(search_result.title)
    source_is_phone = bool(
        amazon_signals["model_family"]
        or re.search(r"\b(?:smartphone|iphone|galaxy|pixel|phone)\b", _combined_search_text(amazon_product), re.I)
    )
    if source_is_phone and candidate_product_type == "accessory":
        conflicts["product_type"] = ["accessory"]
    matched_weight = 0
    applicable_weight = 0

    comparable = ("brand", "family", "model", "cpu", "gpu", "gpu_memory", "ram", "storage", "os", "display", "color")
    for key in comparable:
        source = amazon_signals[key]
        candidate = candidate_signals[key]
        if not source:
            continue
        # Search titles are often truncated or omit optional attributes. A
        # missing candidate value is neutral for optional fields. Core
        # hardware fields still contribute their weight, so a generic family
        # title cannot outrank a candidate with matching hardware.
        if not candidate:
            matched[key] = set()
            if key in {"cpu", "gpu", "gpu_memory", "ram", "storage"} and not candidate_signals["truncated"]:
                applicable_weight += SEARCH_MATCH_WEIGHTS[key]
            continue
        applicable_weight += SEARCH_MATCH_WEIGHTS[key]
        overlap = source & candidate
        matched[key] = overlap
        if overlap:
            # One exact identity/spec value is enough to satisfy the signal;
            # extra marketing/model tokens must not dilute it.
            value = SEARCH_MATCH_WEIGHTS[key]
            component_scores[key] = round(value, 2)
            matched_weight += value
        elif candidate and key in {"cpu", "gpu", "gpu_memory", "ram", "storage", "model"}:
            conflicts[key] = sorted(candidate)

    # Keep explicit contradictions visible even when a marketplace title is
    # truncated; truncation makes missing data neutral, never an alternative
    # value neutral.
    for key in ("cpu", "gpu", "gpu_memory", "ram", "storage", "model"):
        if amazon_signals[key] and candidate_signals[key] and not (
            amazon_signals[key] & candidate_signals[key]
        ):
            conflicts[key] = sorted(candidate_signals[key])

    source_tokens = amazon_signals["tokens"]
    candidate_tokens = candidate_signals["tokens"]
    if source_tokens:
        applicable_weight += SEARCH_MATCH_WEIGHTS["title"]
        title_overlap = source_tokens & candidate_tokens
        matched["title"] = title_overlap
        identity_overlap = any(
            matched.get(key)
            for key in ("model", "cpu", "gpu", "gpu_memory", "ram", "storage")
        )
        value = (
            SEARCH_MATCH_WEIGHTS["title"]
            if identity_overlap and title_overlap
            else SEARCH_MATCH_WEIGHTS["title"] * len(title_overlap) / len(source_tokens)
        )
        component_scores["title"] = round(value, 2)
        matched_weight += value

    score = round(100 * matched_weight / applicable_weight) if applicable_weight else 0
    severe_conflicts = set(conflicts) & {"product_type", "model", "cpu", "gpu", "gpu_memory", "ram", "storage"}
    core_matches = sum(bool(matched.get(key)) for key in ("cpu", "gpu", "ram", "storage"))
    model_match = bool(matched.get("model"))
    if severe_conflicts:
        confidence = MatchConfidence.LOW
        match_status = MatchStatus.REJECTED
        score = min(score, 49)
    elif (model_match or core_matches >= 3) and (core_matches >= 1 or matched.get("family")) and score >= 75:
        confidence = MatchConfidence.HIGH
        match_status = MatchStatus.MATCHED
    elif score >= 65 and (core_matches >= 2 or matched.get("family")):
        confidence = MatchConfidence.MEDIUM
        match_status = MatchStatus.REVIEW
    else:
        confidence = MatchConfidence.LOW
        match_status = MatchStatus.REJECTED

    reasons = []
    labels = {
        "model": "model identifiers",
        "cpu": "CPU models",
        "gpu": "GPU models",
        "ram": "RAM",
        "storage": "storage",
        "brand": "brand",
        "series": "product series",
        "os": "operating system",
    }
    for key, signals in matched.items():
        if signals:
            reasons.append(f"{labels.get(key, 'title')} matched: {', '.join(sorted(signals))}")
    if not reasons:
        reasons.append("no meaningful product identity signals matched")

    return {
        "candidate": search_result,
        "score": score,
        "confidence": confidence,
        "match_status": match_status,
        "reasons": reasons,
        "signals": {
            "matched": {key: sorted(value) for key, value in matched.items()},
            "component_scores": component_scores,
            "conflicts": conflicts,
            "normalized_amazon_title": normalize_text(amazon_product.product_title),
            "normalized_flipkart_title": normalize_text(search_result.title),
            "amazon_attributes": {
                key: sorted(value) for key, value in amazon_signals.items()
                if key not in {"tokens", "truncated"}
            },
            "flipkart_attributes": {
                key: sorted(value) for key, value in candidate_signals.items()
                if key not in {"tokens", "truncated"}
            },
            "flipkart_title_truncated": candidate_signals["truncated"],
            "model_match": model_match,
            "product_type": {
                "amazon": source_product_type,
                "flipkart": candidate_product_type,
            },
            "core_matches": core_matches,
            "applicable_weight": applicable_weight,
            "matched_weight": round(matched_weight, 2),
        },
    }


def rank_flipkart_search_results(amazon_product, search_results) -> list[dict]:
    """Rank all existing candidates, highest score first, deterministically."""
    ranked = [rank_flipkart_search_result(amazon_product, result) for result in search_results]
    return sorted(ranked, key=lambda item: (-item["score"], item["candidate"].position, item["candidate"].pk))


def normalize_capacity(value: str | None) -> str:
    """Normalize values such as 256GB, 256 GB ROM, and 256 gb."""
    match = CAPACITY_PATTERN.search(str(value or ""))
    if not match:
        return normalize_text(value)
    amount = match.group("amount")
    if amount.endswith(".0"):
        amount = amount[:-2]
    return f"{amount} {match.group('unit').casefold()}"


def normalize_color(value: str | None) -> str:
    value = normalize_text(value)
    aliases = {
        "space black": "black",
        "jet black": "black",
        "light-gold": "light gold",
    }
    return aliases.get(value, value)


def first_valid_image_url(product) -> str:
    """Return the first usable extracted image URL without fetching it."""
    images = getattr(product, "images", None)
    if not isinstance(images, (list, tuple)):
        return ""

    for image in images:
        if not isinstance(image, str):
            continue
        image = image.strip()
        parsed = urlparse(image)
        if image and parsed.scheme in {"http", "https"} and parsed.netloc:
            return image
    return ""


def extract_model_identity(product) -> str:
    """Extract a conservative model identity from the product title."""
    title = " ".join((product.product_title or "").split())
    title = re.sub(r"\([^)]*\)", "", title)
    title = re.split(r"\s*[|:;–—]\s*", title, maxsplit=1)[0]
    title = re.split(r"\s+with\s+", title, maxsplit=1, flags=re.IGNORECASE)[0]
    brand = " ".join((product.brand or "").split())
    if brand and title.casefold().startswith(brand.casefold()):
        title = title[len(brand):]

    title = CAPACITY_PATTERN.sub(" ", title)
    color = " ".join((product.color or "").split())
    if color:
        title = re.sub(rf"\b{re.escape(color)}\b", " ", title, flags=re.IGNORECASE)
    return normalize_text(title)


def _field_value(product, field: str) -> str:
    value = getattr(product, field, "")
    if field in {"storage", "ram"}:
        return normalize_capacity(value)
    if field == "color":
        return normalize_color(value)
    return normalize_text(value)


def _comparison(amazon_product, flipkart_product, key, field, hard=False):
    if key == "model":
        amazon_value = extract_model_identity(amazon_product)
        flipkart_value = extract_model_identity(flipkart_product)
    else:
        amazon_value = _field_value(amazon_product, field)
        flipkart_value = _field_value(flipkart_product, field)

    available = bool(amazon_value and flipkart_value)
    matched = available and amazon_value == flipkart_value
    mismatch = available and not matched
    return {
        "matched": matched if available else None,
        "available": available,
        "score": WEIGHTS[key] if matched else 0,
        "amazon": amazon_value,
        "flipkart": flipkart_value,
        "hard_mismatch": bool(hard and mismatch),
    }


def match_products(amazon_product, flipkart_product) -> dict:
    """Return a deterministic match decision without database writes."""
    comparisons = {
        "brand": _comparison(amazon_product, flipkart_product, "brand", "brand", True),
        "model": _comparison(amazon_product, flipkart_product, "model", "product_title", True),
        "storage": _comparison(amazon_product, flipkart_product, "storage", "storage", True),
        "ram": _comparison(amazon_product, flipkart_product, "ram", "ram", True),
        "color": _comparison(amazon_product, flipkart_product, "color", "color"),
        "processor": _comparison(amazon_product, flipkart_product, "processor", "processor"),
        "display": _comparison(
            amazon_product,
            flipkart_product,
            "display",
            "display_size",
        ),
        "other_specs": _comparison(
            amazon_product,
            flipkart_product,
            "other_specs",
            "operating_system",
        ),
    }

    amazon_type = classify_product_type(amazon_product)
    flipkart_type = classify_product_type(flipkart_product)
    if flipkart_type == "accessory" and amazon_type != "accessory":
        comparisons["product_type"] = {
            "matched": False,
            "available": True,
            "score": 0,
            "amazon": amazon_type,
            "flipkart": flipkart_type,
            "hard_mismatch": True,
        }

    # Include resolution as supporting evidence when operating system is absent.
    for product, key in ((amazon_product, "amazon"), (flipkart_product, "flipkart")):
        resolution = normalize_text(getattr(product, "resolution", ""))
        if resolution:
            comparisons["other_specs"][key] = resolution
            comparisons["other_specs"]["available"] = bool(
                comparisons["other_specs"]["amazon"]
                and comparisons["other_specs"]["flipkart"]
            )
            comparisons["other_specs"]["matched"] = (
                comparisons["other_specs"]["available"]
                and comparisons["other_specs"]["amazon"]
                == comparisons["other_specs"]["flipkart"]
            )
            comparisons["other_specs"]["score"] = (
                WEIGHTS["other_specs"]
                if comparisons["other_specs"]["matched"]
                else 0
            )

    applicable_weight = sum(
        WEIGHTS[key]
        for key, reason in comparisons.items()
        if key in WEIGHTS and reason["available"]
    )
    matched_weight = sum(reason["score"] for reason in comparisons.values())
    score = round(matched_weight * 100 / applicable_weight) if applicable_weight else 0

    soft_penalty = 0
    if comparisons["color"]["available"] and not comparisons["color"]["matched"]:
        soft_penalty += 15
    for key in ("processor", "display", "other_specs"):
        if comparisons[key]["available"] and not comparisons[key]["matched"]:
            soft_penalty += 5
    score = max(0, min(100, score - soft_penalty))

    hard_mismatch = any(
        reason["hard_mismatch"] for reason in comparisons.values()
    )
    if hard_mismatch:
        confidence = MatchConfidence.LOW
        match_status = MatchStatus.REJECTED
    elif score >= 85:
        confidence = MatchConfidence.HIGH
        match_status = MatchStatus.MATCHED
    elif score >= 65:
        confidence = MatchConfidence.MEDIUM
        match_status = MatchStatus.REVIEW
    else:
        confidence = MatchConfidence.LOW
        match_status = MatchStatus.REJECTED

    return {
        "score": score,
        "confidence": confidence,
        "match_status": match_status,
        "reasons": {
            **comparisons,
            "summary": {
                "applicable_weight": applicable_weight,
                "matched_weight": matched_weight,
                "hard_mismatch": hard_mismatch,
                "soft_penalty": soft_penalty,
            },
        },
    }


def run_product_matching_for_keyword(search_keyword) -> KeywordMatchingSummary:
    """Match only completed staging products belonging to one keyword.

    Flipkart products are reached through the Amazon products associated with
    the selected keyword. This preserves the importer provenance chain and
    deliberately avoids all search/extraction services.
    """
    amazon_products = list(
        AmazonProduct.objects.filter(
            status=ImportStatus.COMPLETED,
            asin__in=search_keyword.amazon_results.values("asin"),
        ).distinct()
    )
    amazon_ids = [product.pk for product in amazon_products]
    flipkart_products = list(
        FlipkartProduct.objects.filter(
            status=ImportStatus.COMPLETED,
            search_result__amazon_product_id__in=amazon_ids,
        ).select_related("search_result")
    )

    flipkart_by_amazon = {}
    for flipkart_product in flipkart_products:
        flipkart_by_amazon.setdefault(
            flipkart_product.search_result.amazon_product_id, []
        ).append(flipkart_product)

    created = updated = high = medium = low = failed = 0
    no_candidate = 0
    for amazon_product in amazon_products:
        candidates = flipkart_by_amazon.get(amazon_product.pk, [])
        if not candidates:
            no_candidate += 1
            continue
        for flipkart_product in candidates:
            try:
                result = match_products(amazon_product, flipkart_product)
                with transaction.atomic():
                    _, was_created = ProductMatch.objects.update_or_create(
                        amazon_product=amazon_product,
                        flipkart_product=flipkart_product,
                        defaults=result,
                    )
                if was_created:
                    created += 1
                else:
                    updated += 1
                if result["confidence"] == MatchConfidence.HIGH:
                    high += 1
                elif result["confidence"] == MatchConfidence.MEDIUM:
                    medium += 1
                else:
                    low += 1
            except Exception:
                failed += 1

    return KeywordMatchingSummary(
        amazon_products=len(amazon_products),
        flipkart_products=len(flipkart_products),
        matches_created=created,
        matches_updated=updated,
        high_confidence=high,
        medium_confidence=medium,
        low_confidence=low,
        no_candidate=no_candidate,
        failed=failed,
    )


def run_product_matching_for_batch(batch: ImportBatch) -> KeywordMatchingSummary:
    """Match only staged products associated with this ImportBatch."""
    amazon_products = list(
        batch.amazon_products.filter(status=ImportStatus.COMPLETED).distinct()
    )
    amazon_ids = [product.pk for product in amazon_products]
    flipkart_products = list(
        batch.flipkart_products.filter(
            status=ImportStatus.COMPLETED,
            search_result__amazon_product_id__in=amazon_ids,
        ).select_related("search_result")
    )
    by_amazon = {}
    for product in flipkart_products:
        by_amazon.setdefault(product.search_result.amazon_product_id, []).append(product)

    created = updated = high = medium = low = failed = no_candidate = 0
    for amazon_product in amazon_products:
        candidates = by_amazon.get(amazon_product.pk, [])
        if not candidates:
            no_candidate += 1
            continue
        for flipkart_product in candidates:
            try:
                result = match_products(amazon_product, flipkart_product)
                with transaction.atomic():
                    existing = ProductMatch.objects.filter(
                        amazon_product=amazon_product,
                        flipkart_product=flipkart_product,
                    ).first()
                    defaults = dict(result)
                    if existing and existing.match_status in {
                        MatchStatus.APPROVED,
                        MatchStatus.PUBLISHED,
                    }:
                        defaults["match_status"] = existing.match_status
                    match, was_created = ProductMatch.objects.update_or_create(
                        amazon_product=amazon_product,
                        flipkart_product=flipkart_product,
                        defaults=defaults,
                    )
                    match.batches.add(batch)
                if was_created:
                    created += 1
                else:
                    updated += 1
                if result["confidence"] == MatchConfidence.HIGH:
                    high += 1
                elif result["confidence"] == MatchConfidence.MEDIUM:
                    medium += 1
                else:
                    low += 1
            except Exception:
                failed += 1

    return KeywordMatchingSummary(
        amazon_products=len(amazon_products),
        flipkart_products=len(flipkart_products),
        matches_created=created,
        matches_updated=updated,
        high_confidence=high,
        medium_confidence=medium,
        low_confidence=low,
        no_candidate=no_candidate,
        failed=failed,
    )
