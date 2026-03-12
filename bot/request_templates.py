from __future__ import annotations

from pathlib import Path


def template_path_for_listing(settings, listing_id: str) -> Path:
    return settings.request_templates_dir / f"{listing_id}.json"


def should_use_legacy_template(settings, listing_id: str) -> bool:
    return len(settings.listing_ids) <= 1 or (
        bool(settings.listing_ids) and listing_id == settings.listing_ids[0]
    )


def resolve_template_path(settings, listing_id: str) -> Path:
    listing_path = template_path_for_listing(settings, listing_id)
    if listing_path.exists():
        return listing_path

    if should_use_legacy_template(settings, listing_id) and settings.request_template_path.exists():
        return settings.request_template_path

    raise FileNotFoundError(
        "Request template not found for listing "
        f"{listing_id}: expected {listing_path}. "
        f"Run discovery/discover_update_request.py --listing-id {listing_id} first."
    )
