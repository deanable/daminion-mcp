#!/usr/bin/env python3
"""
Daminion MCP Server
FastMCP server exposing 9 tools for Daminion DAM integration.
"""

import os
import sys
import asyncio
import logging
from typing import Optional, List, Dict, Any
from contextlib import asynccontextmanager

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field

# Add src to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.daminion_api import (
    DaminionAPI,
    create_daminion_client,
    DaminionAuthenticationError,
    DaminionRateLimitError,
    DaminionNotFoundError,
    DaminionNetworkError,
    DaminionException,
    DaminionFlagValue,
)

# ── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("daminion-mcp")

# ── Configuration ────────────────────────────────────────────────────────────

DAM_SERVER_URL = os.getenv("DAM_SERVER_URL", "").strip()
DAM_USERNAME = os.getenv("DAM_USERNAME", "").strip()
DAM_PASSWORD = os.getenv("DAM_PASSWORD", "").strip()
DAM_CATALOG_ID = os.getenv("DAM_CATALOG_ID", "").strip()  # Not used by API but kept for compat


def validate_config() -> bool:
    """Check that required environment variables are set."""
    missing = []
    if not DAM_SERVER_URL:
        missing.append("DAM_SERVER_URL")
    if not DAM_USERNAME:
        missing.append("DAM_USERNAME")
    if not DAM_PASSWORD:
        missing.append("DAM_PASSWORD")
    if missing:
        logger.error(f"Missing required environment variables: {', '.join(missing)}")
        return False
    return True


# ── Global Client Instance ──────────────────────────────────────────────────

_daminion_client: Optional[DaminionAPI] = None


async def get_client() -> DaminionAPI:
    """Get or create the Daminion client singleton."""
    global _daminion_client
    if _daminion_client is None or not _daminion_client._authenticated:
        if _daminion_client:
            try:
                await _daminion_client.close()
            except Exception:
                pass
        _daminion_client = await create_daminion_client(
            server_url=DAM_SERVER_URL,
            username=DAM_USERNAME,
            password=DAM_PASSWORD,
        )
    return _daminion_client


@asynccontextmanager
async def lifespan(app: FastMCP):
    """Application lifespan - initialize and cleanup client."""
    # Validate config on startup
    if not validate_config():
        raise RuntimeError("Missing required environment variables. Set DAM_SERVER_URL, DAM_USERNAME, DAM_PASSWORD")

    # Pre-connect to catch auth errors early
    try:
        client = await get_client()
        logger.info("Daminion MCP server initialized successfully")
    except DaminionAuthenticationError as e:
        logger.error(f"Authentication failed: {e}")
        raise
    except Exception as e:
        logger.error(f"Failed to connect to Daminion: {e}")
        raise

    yield

    # Cleanup
    if _daminion_client:
        await _daminion_client.close()
        _daminion_client = None
        logger.info("Daminion client closed")


# ── FastMCP Server ──────────────────────────────────────────────────────────

mcp = FastMCP("daminion", lifespan=lifespan)


# ── Tool 1: search_media_items ──────────────────────────────────────────────

class SearchMediaItemsParams(BaseModel):
    query: Optional[str] = Field(default=None, description="Free-text search query")
    query_line: Optional[str] = Field(default=None, description="Structured query line (tagId,valueId pairs)")
    operators: Optional[str] = Field(default=None, description="Filter operators (e.g., '39,any;45,any')")
    page_size: int = Field(default=100, ge=1, le=500, description="Number of items per page")
    index: int = Field(default=0, ge=0, description="Page index (0-based)")
    status_filter: Optional[str] = Field(default=None, description="Filter by flag: 'approved', 'rejected', 'unassigned'")
    untagged_fields: Optional[List[str]] = Field(default=None, description="Fields that should be empty: 'keywords', 'categories', 'description'")
    scope: str = Field(default="all", description="Scope: 'all', 'search', 'saved_search', 'collection'")
    saved_search_id: Optional[int] = Field(default=None, description="Saved search ID when scope='saved_search'")
    collection_id: Optional[int] = Field(default=None, description="Collection ID when scope='collection'")


@mcp.tool()
async def search_media_items(
    query: Optional[str] = None,
    query_line: Optional[str] = None,
    operators: Optional[str] = None,
    page_size: int = 100,
    index: int = 0,
    status_filter: Optional[str] = None,
    untagged_fields: Optional[List[str]] = None,
    scope: str = "all",
    saved_search_id: Optional[int] = None,
    collection_id: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Search for media items using free-text queries or structured query lines.

    Supports multiple search modes:
    - scope="all": Browse all catalog items (with optional filters)
    - scope="search": Free-text search with optional status/untagged filters
    - scope="saved_search": Execute a saved search by ID
    - scope="collection": Get items from a shared collection by ID
    """
    try:
        client = await get_client()

        # Build search term
        search_term = query

        page = await client.get_items_page(
            scope=scope,
            saved_search_id=saved_search_id,
            collection_id=collection_id,
            search_term=search_term,
            untagged_fields=untagged_fields,
            status_filter=status_filter or "all",
            max_items=page_size,
            start_index=index * page_size,
        )

        items = []
        for item in page.items:
            items.append({
                "id": item.id,
                "fileName": item.fileName,
                "flag": item.flag,
                "keywords": item.Keywords or [],
                "categories": item.Categories or [],
                "description": item.Description,
                "dimensions": item.dimensions,
                "fileSize": item.fileSize,
                "hashCode": item.hashCode,
            })

        return {
            "items": items,
            "count": len(items),
            "totalCount": page.total_count,
            "page": index,
            "pageSize": page_size,
        }
    except DaminionException as e:
        return {"error": str(e), "type": type(e).__name__}
    except Exception as e:
        logger.exception("search_media_items failed")
        return {"error": f"Unexpected error: {e}", "type": "InternalError"}


# ── Tool 2: get_media_item_details ──────────────────────────────────────────

class GetMediaItemDetailsParams(BaseModel):
    item_ids: List[int] = Field(description="Array of media item IDs")
    get_all: bool = Field(default=True, description="Whether to fetch full metadata via ItemData/GetAll")


@mcp.tool()
async def get_media_item_details(
    item_ids: List[int],
    get_all: bool = True,
) -> Dict[str, Any]:
    """
    Retrieve detailed metadata and property panels for specific media items.

    Returns both basic item info and full tag data when get_all=True.
    """
    try:
        client = await get_client()

        if get_all:
            # Fetch full metadata for each item
            results = []
            for item_id in item_ids:
                try:
                    tag_pairs = await client.get_item_tag_pairs(item_id)
                    # Also get basic item info
                    items = await client.get_items_by_ids([item_id])
                    basic = items[0] if items else None

                    results.append({
                        "id": item_id,
                        "basic": {
                            "fileName": basic.fileName if basic else None,
                            "flag": basic.flag if basic else None,
                            "keywords": basic.Keywords if basic else None,
                            "categories": basic.Categories if basic else None,
                            "description": basic.Description if basic else None,
                            "dimensions": basic.dimensions if basic else None,
                            "fileSize": basic.fileSize if basic else None,
                            "hashCode": basic.hashCode if basic else None,
                        } if basic else None,
                        "tags": dict(tag_pairs),
                    })
                except Exception as e:
                    results.append({"id": item_id, "error": str(e)})

            return {"items": results}
        else:
            # Just basic info
            items = await client.get_items_by_ids(item_ids)
            return {
                "items": [
                    {
                        "id": item.id,
                        "fileName": item.fileName,
                        "flag": item.flag,
                        "keywords": item.Keywords or [],
                        "categories": item.Categories or [],
                        "description": item.Description,
                        "dimensions": item.dimensions,
                        "fileSize": item.fileSize,
                        "hashCode": item.hashCode,
                    }
                    for item in items
                ]
            }
    except DaminionException as e:
        return {"error": str(e), "type": type(e).__name__}
    except Exception as e:
        logger.exception("get_media_item_details failed")
        return {"error": f"Unexpected error: {e}", "type": "InternalError"}


# ── Tool 3: get_tag_schema ──────────────────────────────────────────────────

@mcp.tool()
async def get_tag_schema() -> Dict[str, Any]:
    """
    Retrieve the complete tag schema (IDs, GUIDs, indexed vs. linear types).

    This builds a dynamic name-to-ID lookup map from /api/settings/getTags
    and extracts GUIDs from /api/ItemData/GetDefaultLayout.
    """
    try:
        client = await get_client()

        # Return the internal mappings (these are loaded on auth)
        return {
            "tag_guids": dict(client._tag_guid_map),
            "tag_ids": dict(client._tag_id_map),
            "saved_searches_tag_id": client.SAVED_SEARCHES_TAG_FALLBACK_ID,
            "collections_tag_id": client.COLLECTION_TAG_FALLBACK_ID,
        }
    except DaminionException as e:
        return {"error": str(e), "type": type(e).__name__}
    except Exception as e:
        logger.exception("get_tag_schema failed")
        return {"error": f"Unexpected error: {e}", "type": "InternalError"}


# ── Tool 4: get_tag_values ──────────────────────────────────────────────────

class GetTagValuesParams(BaseModel):
    tag_id: int = Field(description="Numeric tag ID (from get_tag_schema)")
    filter_text: Optional[str] = Field(default=None, description="Text filter for values")
    parent_value_id: int = Field(default=-2, description="Parent value ID (-2 = deep search)")
    page_size: int = Field(default=500, ge=1, le=1000, description="Number of values to return")


@mcp.tool()
async def get_tag_values(
    tag_id: int,
    filter_text: Optional[str] = None,
    parent_value_id: int = -2,
    page_size: int = 500,
) -> Dict[str, Any]:
    """
    Fetch values and counts for a specific indexed tag (e.g., Keywords, Categories, Color Label).

    Supports optional text filtering and parent value navigation for hierarchical tags.
    """
    try:
        client = await get_client()

        values = await client.get_tag_values(
            tag_id=tag_id,
            filter_text=filter_text,
            parent_value_id=parent_value_id,
            page_size=page_size,
        )

        # Normalize response
        results = []
        for v in values:
            results.append({
                "id": v.get("id") or v.get("valueId"),
                "text": v.get("text") or v.get("value") or v.get("name") or "",
                "count": v.get("count", 0),
                "raw": v,
            })

        return {"values": results, "count": len(results)}
    except DaminionException as e:
        return {"error": str(e), "type": type(e).__name__}
    except Exception as e:
        logger.exception("get_tag_values failed")
        return {"error": f"Unexpected error: {e}", "type": "InternalError"}


# ── Tool 5: batch_update_tags ───────────────────────────────────────────────

class TagOperation(BaseModel):
    guid: str = Field(description="Tag GUID from get_tag_schema")
    id: Optional[int] = Field(default=None, description="Tag value ID (for indexed tags)")
    value: Optional[str] = Field(default=None, description="Tag value string (for linear tags)")
    remove: bool = Field(default=False, description="Whether to remove the tag")


class BatchUpdateTagsParams(BaseModel):
    item_ids: List[int] = Field(description="Array of media item IDs to update")
    operations: List[TagOperation] = Field(description="Array of tag operations")


@mcp.tool()
async def batch_update_tags(
    item_ids: List[int],
    operations: List[TagOperation],
) -> Dict[str, Any]:
    """
    Assign or remove tags/metadata across multiple media items in bulk.

    Each operation specifies a tag GUID, and either a value ID (for indexed tags)
    or a string value (for linear tags like Description). Set remove=true to delete.
    """
    try:
        client = await get_client()

        ops = [op.model_dump() for op in operations]
        success = await client.batch_update_tags(item_ids, ops)

        return {"success": success, "updatedItems": len(item_ids) if success else 0}
    except DaminionException as e:
        return {"error": str(e), "type": type(e).__name__}
    except Exception as e:
        logger.exception("batch_update_tags failed")
        return {"error": f"Unexpected error: {e}", "type": "InternalError"}


# ── Tool 6: list_shared_collections ─────────────────────────────────────────

@mcp.tool()
async def list_shared_collections() -> Dict[str, Any]:
    """
    List all shared collections available in the catalog with item counts and access codes.
    """
    try:
        client = await get_client()

        collections = await client.get_shared_collections()

        return {
            "collections": [
                {
                    "id": c.id,
                    "name": c.name,
                    "code": c.code,
                    "itemCount": c.item_count,
                }
                for c in collections
            ],
            "count": len(collections),
        }
    except DaminionException as e:
        return {"error": str(e), "type": type(e).__name__}
    except Exception as e:
        logger.exception("list_shared_collections failed")
        return {"error": f"Unexpected error: {e}", "type": "InternalError"}


# ── Tool 7: get_shared_collection_items ─────────────────────────────────────

class GetSharedCollectionItemsParams(BaseModel):
    collection_id: int = Field(description="Shared collection ID from list_shared_collections")
    page_size: int = Field(default=200, ge=1, le=1000, description="Number of items to return")


@mcp.tool()
async def get_shared_collection_items(
    collection_id: int,
    page_size: int = 200,
) -> Dict[str, Any]:
    """
    Retrieve media items belonging to a specific shared collection.

    Uses robust fallbacks (handles empty result bugs via public access codes or range scans).
    """
    try:
        client = await get_client()

        items = await client.get_shared_collection_items(collection_id, page_size)

        return {
            "items": [
                {
                    "id": item.id,
                    "fileName": item.fileName,
                    "flag": item.flag,
                    "keywords": item.Keywords or [],
                    "categories": item.Categories or [],
                    "description": item.Description,
                    "dimensions": item.dimensions,
                    "fileSize": item.fileSize,
                    "hashCode": item.hashCode,
                }
                for item in items
            ],
            "count": len(items),
        }
    except DaminionException as e:
        return {"error": str(e), "type": type(e).__name__}
    except Exception as e:
        logger.exception("get_shared_collection_items failed")
        return {"error": f"Unexpected error: {e}", "type": "InternalError"}


# ── Tool 8: get_export_presets ──────────────────────────────────────────────

@mcp.tool()
async def get_export_presets() -> Dict[str, Any]:
    """
    List available export presets (watermarking, resizing, format conversion rules).
    """
    try:
        client = await get_client()

        presets = await client.get_export_presets()

        return {
            "presets": presets,
            "count": len(presets),
        }
    except DaminionException as e:
        return {"error": str(e), "type": type(e).__name__}
    except Exception as e:
        logger.exception("get_export_presets failed")
        return {"error": f"Unexpected error: {e}", "type": "InternalError"}


# ── Tool 9: create_embed_link ───────────────────────────────────────────────

class CreateEmbedLinkParams(BaseModel):
    item_id: int = Field(description="Media item ID")
    preset_guid: Optional[str] = Field(default=None, description="Optional export preset GUID")


@mcp.tool()
async def create_embed_link(
    item_id: int,
    preset_guid: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Generate a CDN/embed link for a media item using an optional export preset GUID.
    """
    try:
        client = await get_client()

        url = await client.create_embed_link(item_id, preset_guid)

        if url:
            return {"url": url, "itemId": item_id}
        else:
            return {"error": "Failed to create embed link", "type": "DaminionNetworkError"}
    except DaminionException as e:
        return {"error": str(e), "type": type(e).__name__}
    except Exception as e:
        logger.exception("create_embed_link failed")
        return {"error": f"Unexpected error: {e}", "type": "InternalError"}


# ── Bonus: get_saved_searches ───────────────────────────────────────────────

@mcp.tool()
async def list_saved_searches() -> Dict[str, Any]:
    """
    List all saved searches available in the catalog with their item counts.
    """
    try:
        client = await get_client()

        searches = await client.get_saved_searches()

        return {
            "savedSearches": [
                {
                    "id": s.id,
                    "name": s.name,
                    "count": s.count,
                }
                for s in searches
            ],
            "count": len(searches),
        }
    except DaminionException as e:
        return {"error": str(e), "type": type(e).__name__}
    except Exception as e:
        logger.exception("list_saved_searches failed")
        return {"error": f"Unexpected error: {e}", "type": "InternalError"}


# ── Bonus: count_filtered_items ─────────────────────────────────────────────

class CountFilteredItemsParams(BaseModel):
    scope: str = Field(default="all", description="Scope: 'all', 'search', 'saved_search', 'collection'")
    saved_search_id: Optional[int] = Field(default=None)
    collection_id: Optional[int] = Field(default=None)
    search_term: Optional[str] = Field(default=None)
    untagged_fields: Optional[List[str]] = Field(default=None)
    status_filter: str = Field(default="all")


@mcp.tool()
async def count_filtered_items(
    scope: str = "all",
    saved_search_id: Optional[int] = None,
    collection_id: Optional[int] = None,
    search_term: Optional[str] = None,
    untagged_fields: Optional[List[str]] = None,
    status_filter: str = "all",
) -> Dict[str, Any]:
    """
    Count items matching filters (for pickers/step-1 selection).
    Returns -1 on failure.
    """
    try:
        client = await get_client()

        count = await client.get_filtered_item_count(
            scope=scope,
            saved_search_id=saved_search_id,
            collection_id=collection_id,
            search_term=search_term,
            untagged_fields=untagged_fields,
            status_filter=status_filter,
        )

        return {"count": count}
    except DaminionException as e:
        return {"error": str(e), "type": type(e).__name__}
    except Exception as e:
        logger.exception("count_filtered_items failed")
        return {"error": f"Unexpected error: {e}", "type": "InternalError"}


# ── Main Entry Point ────────────────────────────────────────────────────────

if __name__ == "__main__":
    # FastMCP handles stdio transport automatically
    mcp.run()