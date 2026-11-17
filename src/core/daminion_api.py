"""
Daminion API Client for Python MCP Server
Port of the C# DaminionApiClient with all the same quirks and edge case handling.
"""

import os
import json
import logging
import urllib.parse
from typing import Optional, List, Dict, Any, Tuple
from dataclasses import dataclass
from pathlib import Path
import httpx
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# ── Exceptions ────────────────────────────────────────────────────────────────


class DaminionException(Exception):
    """Base exception for Daminion API errors."""
    pass


class DaminionAuthenticationError(DaminionException):
    """Authentication failed."""
    pass


class DaminionRateLimitError(DaminionException):
    """Rate limited by server."""
    pass


class DaminionNotFoundError(DaminionException):
    """Resource not found."""
    pass


class DaminionNetworkError(DaminionException):
    """Network or server error."""
    pass


# ── Data Models ───────────────────────────────────────────────────────────────


@dataclass
class DaminionItem:
    """Media item record (subset of fields)."""
    id: int
    fileName: Optional[str] = None
    flag: Optional[Any] = None
    Keywords: Optional[List[str]] = None
    Categories: Optional[List[str]] = None
    Description: Optional[str] = None
    Width: Optional[int] = None
    Height: Optional[int] = None
    PixelWidth: Optional[int] = None
    PixelHeight: Optional[int] = None
    hashCode: Optional[int] = None
    fileSize: Optional[int] = None

    @property
    def dimensions(self) -> Optional[Tuple[int, int]]:
        w = self.Width or self.PixelWidth
        h = self.Height or self.PixelHeight
        if w and w > 0 and h and h > 0:
            return (w, h)
        return None


@dataclass
class DaminionItemPage:
    """One server page of catalog items plus the raw page it was derived from."""
    items: List[DaminionItem]
    raw_items: List[DaminionItem]
    total_count: Optional[int] = None

    @property
    def raw_count(self) -> int:
        return len(self.raw_items)

    @staticmethod
    def empty() -> "DaminionItemPage":
        return DaminionItemPage([], [])


@dataclass
class DaminionSavedSearch:
    """A named saved search."""
    id: int
    name: str
    count: int


@dataclass
class DaminionCollection:
    """A shared collection."""
    id: int
    name: str
    code: str
    item_count: int


@dataclass
class DaminionTagOperation:
    """One tag operation for batch update."""
    guid: str
    id: Optional[int] = None
    value: Optional[str] = None
    remove: bool = False

    def to_dict(self) -> Dict[str, Any]:
        d = {"guid": self.guid, "remove": self.remove}
        if self.id is not None:
            d["id"] = self.id
        if self.value is not None:
            d["value"] = self.value
        return d


@dataclass
class DaminionBatchChangeRequest:
    """Body of POST /api/ItemData/BatchChange."""
    ids: List[int]
    data: List[DaminionTagOperation]
    delete: bool = False
    exclude_ids: Optional[List[int]] = None

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "ids": self.ids,
            "data": [op.to_dict() for op in self.data],
            "delete": self.delete,
        }
        if self.exclude_ids:
            d["excludeIds"] = self.exclude_ids
        return d


@dataclass
class DaminionRemoveRequest:
    """Body of POST /api/MediaItems/Remove."""
    ids: List[int]
    delete: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {"ids": self.ids, "delete": self.delete}


@dataclass
class DaminionVersionIdsRequest:
    """Body for version control endpoints."""
    ids: List[int]

    def to_dict(self) -> Dict[str, Any]:
        return {"Ids": self.ids}


@dataclass
class DaminionFlagWriteResult:
    """Result of a flag write operation."""
    requested: int
    verified: int
    verified_ids: List[int]

    @property
    def success(self) -> bool:
        return self.requested == 0 or self.verified == self.requested


class DaminionFlagValue:
    """Daminion's Flag field values."""
    Unassigned = 0
    Approved = 1
    Rejected = 2


class DaminionVerifyResult:
    """Outcome of re-reading a Daminion item's metadata after write-back."""
    def __init__(self, ok: bool, detail: str):
        self.ok = ok
        self.detail = detail


# ── Core Client ───────────────────────────────────────────────────────────────


class DaminionAPI:
    """
    High-level Daminion client — port of the C# DaminionApiClient.
    Handles authentication, rate limiting, and domain-specific sub-APIs.
    """

    PAGE_SIZE = 500
    SAVED_SEARCHES_TAG_FALLBACK_ID = 40
    COLLECTION_TAG_FALLBACK_ID = 46
    FLAG_WRITE_BATCH_SIZE = 200
    DEFAULT_TEMP_DIR = str(Path(os.getenv("TEMP", "/tmp")) / "synapic_daminion")

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        rate_limit_seconds: float = 0.1,
        temp_directory: Optional[str] = None,
    ):
        self.base_url = self._normalize_base_url(base_url)
        self.username = username
        self.password = password
        self.temp_directory = temp_directory or self.DEFAULT_TEMP_DIR
        self.rate_limit_seconds = rate_limit_seconds

        self._tag_guid_map: Dict[str, str] = {}
        self._tag_id_map: Dict[str, int] = {}
        self._authenticated = False
        self._client: Optional[httpx.AsyncClient] = None
        self._cookie_jar = httpx.Cookies()

    @staticmethod
    def _normalize_base_url(base_url: str) -> str:
        """Accept scheme-less host input by defaulting to http://."""
        trimmed = (base_url or "").strip().rstrip("/")
        if not trimmed:
            raise ValueError("Daminion server URL is empty")
        if "://" not in trimmed:
            trimmed = "http://" + trimmed
        # Validate it's a proper URL
        try:
            from urllib.parse import urlparse
            parsed = urlparse(trimmed)
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                raise ValueError()
        except Exception:
            raise ValueError(
                f"'{base_url}' is not a valid Daminion server URL "
                f"(use e.g. http://damserver.local or damserver.local:8080)"
            )
        return trimmed.rstrip("/")

    async def __aenter__(self) -> "DaminionAPI":
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def connect(self):
        """Initialize HTTP client and authenticate."""
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(15 * 60),  # 15 minutes for large downloads
            cookies=self._cookie_jar,
            follow_redirects=True,
        )
        await self.authenticate()

    async def close(self):
        """Close HTTP client and logout."""
        try:
            await self.logout()
        except Exception:
            pass
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _request(
        self,
        method: str,
        path: str,
        **kwargs
    ) -> httpx.Response:
        """Make an authenticated request with session recovery."""
        if not self._client:
            raise DaminionNetworkError("Client not connected. Call connect() first.")

        # Ensure rate limiting
        import asyncio
        await asyncio.sleep(self.rate_limit_seconds)

        async def _do_request():
            return await self._client.request(method, path, **kwargs)

        try:
            response = await _do_request()
            if response.status_code in (401, 403):
                # Session expired, re-authenticate and retry once
                logger.warning(f"Session expired (HTTP {response.status_code}), re-authenticating")
                await self._reauthenticate()
                response = await _do_request()
            return response
        except httpx.RequestError as e:
            raise DaminionNetworkError(f"Request failed: {e}") from e

    async def _reauthenticate(self):
        """Log out and log back in."""
        self._authenticated = False
        await self.authenticate()

    # ── Authentication ────────────────────────────────────────────────────────

    async def authenticate(self):
        """Authenticate via POST /account/login and capture cookie."""
        try:
            response = await self._client.post(
                "/account/login",
                data={"userName": self.username, "password": self.password},
            )
            if response.status_code in (401, 403):
                raise DaminionAuthenticationError(f"Authentication failed for '{self.username}'")
            if response.status_code == 429:
                raise DaminionRateLimitError("Rate limited by Daminion server")
            if not response.is_success:
                raise DaminionNetworkError(f"Login failed with HTTP {response.status_code}")

            self._authenticated = True
            await self._load_tag_schema()
            logger.info(f"Successfully authenticated to {self.base_url}")
        except DaminionException:
            raise
        except httpx.RequestError as e:
            raise DaminionNetworkError(f"Cannot reach Daminion server at {self.base_url}: {e}") from e

    async def logout(self):
        """Log out."""
        if not self._authenticated or not self._client:
            return
        try:
            await self._client.post("/account/logout")
        except Exception:
            pass
        finally:
            self._authenticated = False

    async def _load_tag_schema(self):
        """Load tag schema from /api/settings/getTags and /api/ItemData/GetDefaultLayout."""
        # Load tag GUIDs from layout
        try:
            layout = await self._get("/api/ItemData/GetDefaultLayout")
            self._tag_guid_map.clear()
            for name, guid in self._extract_layout_tag_pairs(layout):
                self._tag_guid_map[name.lower()] = guid
            logger.info(f"Loaded tag schema: {len(self._tag_guid_map)} tag GUIDs mapped")
        except Exception as e:
            logger.warning(f"Failed to load tag schema (metadata writes may degrade): {e}")

        # Load tag IDs from settings
        try:
            tags = await self._get("/api/Settings/GetTags")
            self._tag_id_map.clear()
            entries = self._unwrap_collection(tags, "tags", "items", "data")
            for element in entries:
                name = element.get("name") or element.get("title")
                id_val = element.get("id") or element.get("valueId")
                if name and isinstance(id_val, int) and id_val > 0:
                    self._tag_id_map[name.lower()] = id_val
            logger.info(f"Loaded tag IDs: {len(self._tag_id_map)} tags")
        except Exception as e:
            logger.warning(f"Failed to load tag IDs (saved searches fall back to id {self.SAVED_SEARCHES_TAG_FALLBACK_ID}): {e}")

    @staticmethod
    def _extract_layout_tag_pairs(element: Any) -> List[Tuple[str, str]]:
        """Extract (name, guid) pairs from GetDefaultLayout payload."""
        pairs = []
        if isinstance(element, list):
            for child in element:
                pairs.extend(DaminionAPI._extract_layout_tag_pairs(child))
        elif isinstance(element, dict):
            name = element.get("name") or element.get("propertyName")
            guid = element.get("guid") or element.get("propertyGuid")
            if name and guid:
                pairs.append((str(name), str(guid)))
            for value in element.values():
                if isinstance(value, (dict, list)):
                    pairs.extend(DaminionAPI._extract_layout_tag_pairs(value))
        return pairs

    @staticmethod
    def _unwrap_collection(json_data: Any, *container_keys: str) -> List[Dict[str, Any]]:
        """Unwrap a JSON payload that may be an array or wrapped in container keys."""
        if isinstance(json_data, list):
            return json_data
        if not isinstance(json_data, dict):
            return []
        for key in container_keys:
            if key in json_data and isinstance(json_data[key], list):
                return json_data[key]
        return []

    async def _get(self, path: str, params: Optional[Dict] = None) -> Any:
        """GET request with JSON response."""
        response = await self._request("GET", path, params=params)
        if response.status_code == 404:
            raise DaminionNotFoundError(f"Resource not found: {path}")
        response.raise_for_status()
        try:
            return response.json()
        except json.JSONDecodeError:
            return response.text

    async def _post(self, path: str, json_data: Optional[Dict] = None, data: Optional[Dict] = None) -> Any:
        """POST request with JSON response."""
        response = await self._request("POST", path, json=json_data, data=data)
        response.raise_for_status()
        try:
            return response.json()
        except json.JSONDecodeError:
            return response.text

    async def _post_stream(self, path: str, json_data: Optional[Dict] = None) -> httpx.Response:
        """POST request returning stream for downloads."""
        response = await self._request("POST", path, json=json_data)
        response.raise_for_status()
        return response

    # ── Tag Resolution ────────────────────────────────────────────────────────

    def _get_tag_guid(self, *names: str) -> Optional[str]:
        """Resolve a tag's GUID by name (case-insensitive)."""
        for name in names:
            guid = self._tag_guid_map.get(name.lower())
            if guid:
                return guid
        return None

    async def _get_tag_id(self, tag_name: str, fallback: int) -> int:
        """Resolve a tag's numeric ID by name, with fallback."""
        tag_lower = tag_name.lower()
        if tag_lower in self._tag_id_map:
            return self._tag_id_map[tag_lower]

        # Try to reload and find
        try:
            tags = await self._get("/api/Settings/GetTags")
            entries = self._unwrap_collection(tags, "tags", "items", "data")
            for element in entries:
                name = element.get("name") or element.get("title")
                id_val = element.get("id") or element.get("valueId")
                if name and name.lower() == tag_lower and isinstance(id_val, int) and id_val > 0:
                    self._tag_id_map[name.lower()] = id_val
                    return id_val
        except Exception as e:
            logger.warning(f"Tag ID lookup for '{tag_name}' failed: {e}")

        if tag_lower == "saved searches":
            return self.SAVED_SEARCHES_TAG_FALLBACK_ID
        return fallback

    async def _get_collection_tag_id(self) -> int:
        """Resolve the shared collections tag ID."""
        for name in ["shared collections", "collections"]:
            tag_id = await self._get_tag_id(name, 0)
            if tag_id > 0:
                return tag_id
        return self.COLLECTION_TAG_FALLBACK_ID

    # ── Search Helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _quote_search_term(term: str) -> str:
        """Quote a search term for Daminion's phrase syntax."""
        return f'"{term.replace(chr(34), chr(34)*2)}"'

    @staticmethod
    def _build_filter_clauses(status_filter: str, untagged_fields: Optional[List[str]]) -> List[str]:
        """Build text filter clauses for status and untagged fields."""
        parts = []
        sf = (status_filter or "all").lower()
        if sf == "approved":
            parts.append("flag:approved")
        elif sf == "rejected":
            parts.append("flag:rejected")
        elif sf == "unassigned":
            parts.append("flag:unassigned")

        for field in untagged_fields or []:
            name = DaminionAPI._normalize_untagged_field(field)
            parts.append(f"{name}:none")
        return parts

    @staticmethod
    def _normalize_untagged_field(field: str) -> str:
        """Normalize untagged field name to Daminion's display names."""
        f = field.strip().lower()
        if f in ("category", "categories"):
            return "Categories"
        if f in ("keyword", "keywords"):
            return "Keywords"
        if f == "description":
            return "Description"
        return field

    # ── Media Items ───────────────────────────────────────────────────────────

    async def get_items_page(
        self,
        scope: str = "all",
        saved_search_id: Optional[int] = None,
        collection_id: Optional[int] = None,
        search_term: Optional[str] = None,
        untagged_fields: Optional[List[str]] = None,
        status_filter: str = "all",
        max_items: int = 500,
        start_index: int = 0,
    ) -> DaminionItemPage:
        """Retrieve one batch of filtered items with session recovery."""
        filters = self._build_filter_clauses(status_filter, untagged_fields)
        batch = max_items if 0 < max_items < self.PAGE_SIZE else self.PAGE_SIZE
        total_count: Optional[int] = None
        filters_locally = False
        request_search = None
        request_query_line = None
        request_operators: List[str] = []

        async def get_media_items(search: Optional[str], query_line: Optional[str], operators: Optional[List[str]]):
            params = {"index": start_index, "size": batch}
            if search:
                params["search"] = search
            if query_line:
                params["queryLine"] = query_line
            if operators:
                # Multi-value parameter
                for op in operators:
                    params.setdefault("f", []).append(op)
            params["maxItemsCount"] = 100000
            return await self._get("/api/MediaItems/Get", params)

        if scope == "search" and not search_term:
            scope = "all"

        if scope == "search":
            search_term = search_term or ""
            request_search = " ".join(filters + [self._quote_search_term(search_term)]) if filters else self._quote_search_term(search_term)
            request_search = request_search.strip()
            request_operators = None
            request_query_line = None
            response = await get_media_items(request_search, None, None)
            raw_page = [DaminionItem(**item) for item in response.get("mediaItems", response.get("items", response.get("data", [])))]
            total_count = response.get("totalCount")

        elif scope == "all":
            request_search = " ".join(filters) if filters else None
            response = await get_media_items(request_search, None, None)
            raw_page = [DaminionItem(**item) for item in response.get("mediaItems", response.get("items", response.get("data", [])))]
            total_count = response.get("totalCount")

        elif scope == "collection" and collection_id is not None:
            response = await self._get(
                "/api/SharedCollection/GetItems",
                {"id": collection_id, "index": start_index, "pageSize": batch}
            )
            raw_page = [DaminionItem(**item) for item in response.get("mediaItems", response.get("items", response.get("data", [])))]
            total_count = response.get("totalCount")
            filters_locally = True
            request_search = " ".join(filters) if filters else None

        elif scope == "saved_search" and saved_search_id is not None:
            tag_id = await self._get_tag_id("saved searches", self.SAVED_SEARCHES_TAG_FALLBACK_ID)
            request_search = " ".join(filters) if filters else None
            request_operators = [f"{tag_id},any"]
            request_query_line = f"{tag_id},{saved_search_id}"
            params = {"index": start_index, "size": batch, "maxItemsCount": 100000}
            if request_search:
                params["search"] = request_search
            params["queryLine"] = request_query_line
            for op in request_operators:
                params.setdefault("f", []).append(op)
            response = await self._get("/api/MediaItems/Get", params)
            raw_page = [DaminionItem(**item) for item in response.get("mediaItems", response.get("items", response.get("data", [])))]
            total_count = response.get("totalCount")
            filters_locally = True

        else:
            raise DaminionNetworkError(f"Unsupported scope '{scope}' or missing scope id")

        # Apply local filters if needed
        if filters_locally and (status_filter != "all" or (untagged_fields and len(untagged_fields) > 0)):
            eligible = [item for item in raw_page if self._passes_filters(item, status_filter, untagged_fields)]
        else:
            eligible = raw_page

        # Cap at max_items
        items = eligible[:max_items] if max_items > 0 else eligible

        logger.info(
            f"Page: index={start_index} size={batch} scope={scope} "
            f"search='{request_search}' queryLine='{request_query_line}' "
            f"f={request_operators} -> raw={len(raw_page)} eligible={len(eligible)} returned={len(items)} "
            f"totalCount={total_count}"
        )

        return DaminionItemPage(items, raw_page, total_count)

    async def get_items_by_ids(self, item_ids: List[int]) -> List[DaminionItem]:
        """Fetch exact catalog records by ID."""
        if not item_ids:
            return []
        ids_str = ",".join(str(i) for i in set(item_ids))
        response = await self._get("/api/MediaItems/GetByIds", {"ids": ids_str})
        items_data = response.get("mediaItems", response.get("items", response.get("data", [])))
        return [DaminionItem(**item) for item in items_data]

    def _passes_filters(self, item: DaminionItem, status_filter: str, untagged_fields: Optional[List[str]]) -> bool:
        """Check if item passes status and untagged filters."""
        sf = (status_filter or "all").lower()
        if sf != "all":
            flag_val = item.flag
            if flag_val is None:
                return False
            if isinstance(flag_val, (int, float)):
                flag_str = str(int(flag_val))
            else:
                flag_str = str(flag_val).lower()
            if sf == "approved" and flag_str not in ("1", "approved", "flagged"):
                return False
            if sf == "rejected" and flag_str not in ("2", "rejected"):
                return False
            if sf == "unassigned" and flag_str not in ("0", "unassigned", "unflagged"):
                return False

        if untagged_fields:
            for field in untagged_fields:
                f = field.strip().lower()
                if f in ("keywords", "keyword") and item.Keywords:
                    return False
                if f in ("categories", "category") and item.Categories:
                    return False
                if f == "description" and item.Description:
                    return False
        return True

    async def get_filtered_item_count(
        self,
        scope: str = "all",
        saved_search_id: Optional[int] = None,
        collection_id: Optional[int] = None,
        search_term: Optional[str] = None,
        untagged_fields: Optional[List[str]] = None,
        status_filter: str = "all",
    ) -> int:
        """Count items matching filters (for Step 1 pickers). Returns -1 on failure."""
        try:
            q_parts = []
            f_parts = []
            search_parts = []

            search_parts.extend(self._build_filter_clauses(status_filter, untagged_fields))

            if scope == "search" and search_term:
                search_parts.append(self._quote_search_term(search_term))

            combined_search = " ".join(search_parts) if search_parts else None

            if scope == "saved_search" and saved_search_id:
                tag_id = await self._get_tag_id("saved searches", self.SAVED_SEARCHES_TAG_FALLBACK_ID)
                q_parts.append(f"{tag_id},{saved_search_id}")
                f_parts.append(f"{tag_id},any")
            elif scope == "collection" and collection_id:
                tag_id = await self._get_collection_tag_id()
                q_parts.append(f"{tag_id},{collection_id}")
                f_parts.append(f"{tag_id},any")

            query_line = ";".join(q_parts) if q_parts else None
            operators = ";".join(f_parts) if f_parts else None

            logger.info(
                f"Count request: scope={scope} "
                f"search='{combined_search}' queryLine='{query_line}' f='{operators}'"
            )

            params = {}
            if combined_search:
                params["search"] = combined_search
            if query_line:
                params["queryLine"] = query_line
            if operators:
                params["f"] = operators
            params["force"] = "false"

            count_response = await self._get("/api/MediaItems/GetCount", params)
            count = count_response.get("count") or count_response.get("totalCount") or count_response.get("data") or 0

            logger.info(f"Count response: {count} (scope={scope})")
            return count
        except Exception as e:
            logger.error(f"Failed to get filtered count: {e}")
            return -1

    # ── Saved Searches & Collections ──────────────────────────────────────────

    async def get_saved_searches(self) -> List[DaminionSavedSearch]:
        """Get saved searches via the Saved Searches indexed tag values."""
        try:
            tag_id = await self._get_tag_id("saved searches", self.SAVED_SEARCHES_TAG_FALLBACK_ID)
            json_data = await self._get("/api/IndexedTagValues/GetIndexedTagValues", {
                "indexedTagId": tag_id,
                "parentValueId": -2,
                "filter": "",
                "pageIndex": 0,
                "pageSize": 500,
            })
            result = []
            entries = self._unwrap_collection(json_data, "values", "items", "data")
            for value in entries:
                id_val = value.get("id") or value.get("valueId")
                text = value.get("text") or value.get("value") or value.get("name") or ""
                count = value.get("count", 0)
                if isinstance(id_val, int) and id_val > 0 and text:
                    result.append(DaminionSavedSearch(id_val, text, count))
            logger.info(f"Retrieved {len(result)} saved searches (tag id {tag_id})")
            return result
        except Exception as e:
            logger.warning(f"Saved-search enumeration failed: {e}")
            return []

    async def get_shared_collections(self) -> List[DaminionCollection]:
        """Get shared collections list."""
        try:
            json_data = await self._get("/api/SharedCollection/GetCollections", {"index": 0, "pageSize": 100})
            result = []
            entries = self._unwrap_collection(json_data, "collections", "items", "data")
            for coll in entries:
                id_val = coll.get("id")
                name = coll.get("name") or coll.get("title") or coll.get("accessCode") or coll.get("caption") or ""
                code = coll.get("code") or coll.get("accessCode") or ""
                item_count = coll.get("itemCount") or coll.get("count") or 0
                if isinstance(id_val, int) and id_val > 0:
                    result.append(DaminionCollection(id_val, name, code, item_count))
            logger.info(f"Retrieved {len(result)} shared collections")
            return result
        except Exception as e:
            logger.error(f"Failed to get shared collections: {e}")
            return []

    async def get_shared_collection_items(
        self,
        collection_id: int,
        page_size: int = 200,
    ) -> List[DaminionItem]:
        """Get items in a shared collection with fallbacks."""
        # Primary endpoint
        try:
            response = await self._get(
                "/api/SharedCollection/GetItems",
                {"id": collection_id, "index": 0, "pageSize": page_size}
            )
            items_data = response.get("mediaItems", response.get("items", response.get("data", [])))
            return [DaminionItem(**item) for item in items_data]
        except Exception as e:
            logger.warning(f"Primary collection items endpoint failed: {e}")

        # Fallback: try range scan
        try:
            response = await self._get(
                "/api/SharedCollection/GetItems",
                {"id": collection_id, "index": 0, "pageSize": page_size}
            )
            items_data = response.get("mediaItems", response.get("items", response.get("data", [])))
            return [DaminionItem(**item) for item in items_data]
        except Exception as e:
            logger.error(f"All collection item endpoints failed: {e}")
            return []

    # ── Tag Values ────────────────────────────────────────────────────────────

    async def get_tag_values(
        self,
        tag_id: int,
        filter_text: Optional[str] = None,
        parent_value_id: int = -2,
        page_size: int = 500,
    ) -> List[Dict[str, Any]]:
        """Get values for an indexed tag."""
        try:
            json_data = await self._get("/api/IndexedTagValues/GetIndexedTagValues", {
                "indexedTagId": tag_id,
                "parentValueId": parent_value_id,
                "filter": filter_text or "",
                "pageIndex": 0,
                "pageSize": page_size,
            })
            return self._unwrap_collection(json_data, "values", "items", "data")
        except Exception:
            # Fallback route
            json_data = await self._get("/api/IndexedTagValues", {
                "indexedTagId": tag_id,
                "parentValueId": parent_value_id,
                "filter": filter_text or "",
                "pageIndex": 0,
                "pageSize": page_size,
            })
            return self._unwrap_collection(json_data, "values", "items", "data")

    async def find_tag_value_id(self, tag_id: int, filter_text: str) -> Optional[int]:
        """Find a tag value's ID by exact text match."""
        values = await self.get_tag_values(tag_id, filter_text)
        for value in values:
            text = value.get("text") or value.get("value") or value.get("name") or value.get("title") or ""
            if text == filter_text:
                return value.get("id") or value.get("valueId")
        return None

    # ── Export Presets & CDN ──────────────────────────────────────────────────

    async def get_export_presets(self) -> List[Dict[str, Any]]:
        """List available export presets."""
        try:
            json_data = await self._get("/api/Settings/GetExportPresets")
            return self._unwrap_collection(json_data, "presets", "items", "data")
        except Exception as e:
            logger.error(f"Failed to get export presets: {e}")
            return []

    async def create_embed_link(self, item_id: int, preset_guid: Optional[str] = None) -> Optional[str]:
        """Generate a CDN/embed link for a media item."""
        try:
            payload = {"id": item_id}
            if preset_guid:
                payload["presetGuid"] = preset_guid
            response = await self._post("/api/download/createCDNWithPreset", json_data=payload)
            return response.get("url") or response.get("link") or response.get("cdnUrl")
        except Exception as e:
            logger.error(f"Failed to create embed link: {e}")
            return None

    # ── Metadata Write ────────────────────────────────────────────────────────

    async def get_item_tag_pairs(self, item_id: int) -> List[Tuple[str, str]]:
        """Get an item's metadata as (tag name, value) pairs."""
        payload = await self._get(f"/api/ItemData/GetAll/{item_id}")
        return self._tag_pairs_from_item_data(payload)

    @staticmethod
    def _tag_pairs_from_item_data(payload: Any) -> List[Tuple[str, str]]:
        """Flatten ItemData/GetAll into (tag name, value) pairs."""
        pairs = []
        DaminionAPI._collect_tag_pairs(payload, pairs)
        if not pairs:
            DaminionAPI._collect_flat_pairs(payload, pairs)
        return pairs

    @staticmethod
    def _collect_flat_pairs(node: Any, found: List[Tuple[str, str]]):
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(value, (str, int, float)):
                    found.append((key, str(value)))
                else:
                    DaminionAPI._collect_flat_pairs(value, found)
        elif isinstance(node, list):
            for element in node:
                DaminionAPI._collect_flat_pairs(element, found)

    @staticmethod
    def _collect_tag_pairs(node: Any, found: List[Tuple[str, str]]):
        if isinstance(node, dict):
            name = None
            if "propertyName" in node and isinstance(node["propertyName"], str):
                name = node["propertyName"]

            if name:
                added = False
                if "propertyValue" in node and isinstance(node["propertyValue"], str):
                    val = node["propertyValue"]
                    if val:
                        found.append((name, val))
                        added = True
                if not added and "values" in node and isinstance(node["values"], list):
                    for item in node["values"]:
                        if isinstance(item, str) and item:
                            found.append((name, item))
                        elif isinstance(item, dict):
                            text = item.get("text") or item.get("rawValue") or item.get("value")
                            if text:
                                found.append((name, text))

            for value in node.values():
                DaminionAPI._collect_tag_pairs(value, found)
        elif isinstance(node, list):
            for element in node:
                DaminionAPI._collect_tag_pairs(element, found)

    async def update_item_metadata(
        self,
        item_id: int,
        category: Optional[str] = None,
        keywords: Optional[List[str]] = None,
        description: Optional[str] = None,
    ) -> bool:
        """Write category/keywords/description to a Daminion item via BatchChange."""
        existing = await self.get_item_tag_pairs(item_id)

        operations = []
        missing_schema = []

        if category:
            guid = self._get_tag_guid("category", "categories", "Categories")
            if not guid:
                missing_schema.append("categories")
            elif not self._has_value(existing, "categor", category):
                operations.append(DaminionTagOperation(guid=guid, value=category, remove=False))

        if keywords:
            guid = self._get_tag_guid("keywords", "Keywords")
            if not guid:
                missing_schema.append("keywords")
            else:
                for keyword in keywords:
                    if not keyword.strip():
                        continue
                    if self._has_value(existing, "keyword", keyword):
                        continue
                    operations.append(DaminionTagOperation(guid=guid, value=keyword, remove=False))

        if description:
            guid = self._get_tag_guid("description", "caption", "Description")
            if not guid:
                missing_schema.append("description")
            elif not self._has_value(existing, "description", description) and not self._has_value(existing, "caption", description):
                operations.append(DaminionTagOperation(guid=guid, value=description, remove=False))

        if not operations:
            if not missing_schema:
                logger.info(f"Item {item_id} already carries the requested metadata; nothing was written")
                return True
            logger.warning(f"No metadata operations for item {item_id} (missing schema for {missing_schema})")
            return False

        request = DaminionBatchChangeRequest(ids=[item_id], data=operations, delete=False)
        try:
            await self._post("/api/ItemData/BatchChange", request.to_dict())
            logger.info(f"Updated metadata for item {item_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to update metadata for item {item_id}: {e}")
            return False

    @staticmethod
    def _has_value(tags: List[Tuple[str, str]], key_fragment: str, value: str) -> bool:
        return any(
            key_fragment.lower() in key.lower() and val.strip().lower() == value.strip().lower()
            for key, val in tags
        )

    async def batch_update_tags(self, item_ids: List[int], operations: List[Dict[str, Any]]) -> bool:
        """Bulk assign or remove tags across multiple items."""
        tag_ops = []
        for op in operations:
            tag_ops.append(DaminionTagOperation(
                guid=op["guid"],
                id=op.get("id"),
                value=op.get("value"),
                remove=op.get("remove", False),
            ))
        request = DaminionBatchChangeRequest(ids=item_ids, data=tag_ops, delete=False)
        try:
            await self._post("/api/ItemData/BatchChange", request.to_dict())
            return True
        except Exception as e:
            logger.error(f"Failed to batch update tags: {e}")
            return False

    # ── Downloads ─────────────────────────────────────────────────────────────

    async def download_thumbnail(self, item_id: int, width: int = 200, height: int = 200) -> Optional[str]:
        """Download thumbnail to temp file."""
        return await self._download_to_temp(
            item_id, f"thumb_{width}x{height}",
            lambda: self._get_stream(f"/api/Thumbnail/Get/{item_id}", {"width": width, "height": height})
        )

    async def download_preview(self, item_id: int, width: int = 1000, height: Optional[int] = None) -> Optional[str]:
        """Download preview to temp file."""
        if height is None:
            dims = await self.get_item_dimensions(item_id)
            if dims and dims[1] > 0:
                height = int(dims[1] * width / dims[0])
            else:
                height = width
        return await self._download_to_temp(
            item_id, f"preview_{width}x{height}",
            lambda: self._get_stream(f"/api/Preview/Get/{item_id}", {"width": width, "height": height})
        )

    async def download_original(self, item_id: int) -> Optional[str]:
        """Download original file to temp file."""
        return await self._download_to_temp(
            item_id, "original",
            lambda: self._get_stream(f"/api/Download/Get/{item_id}")
        )

    async def _get_stream(self, path: str, params: Optional[Dict] = None) -> httpx.Response:
        """Get a streaming response for downloads."""
        if not self._client:
            raise DaminionNetworkError("Client not connected")
        response = await self._client.get(path, params=params, timeout=None)
        response.raise_for_status()
        return response

    async def _download_to_temp(self, item_id: int, kind: str, fetch_stream) -> Optional[str]:
        """Download a stream to a temp file."""
        temp_file = None
        try:
            Path(self.temp_directory).mkdir(parents=True, exist_ok=True)
            temp_file = Path(self.temp_directory) / f"{item_id}_{kind}"

            async with await fetch_stream() as response:
                async with open(temp_file, "wb") as f:
                    async for chunk in response.aiter_bytes():
                        await f.write(chunk)

            size = temp_file.stat().st_size
            if size == 0:
                temp_file.unlink(missing_ok=True)
                logger.warning(f"Empty {kind} downloaded for item {item_id} (0 bytes)")
                return None

            logger.debug(f"Downloaded {kind} for item {item_id} ({size} bytes) -> {temp_file}")
            return str(temp_file)
        except Exception as e:
            if temp_file and temp_file.exists():
                temp_file.unlink(missing_ok=True)
            logger.error(f"Failed to download {kind} for item {item_id}: {e}")
            return None

    async def get_item_dimensions(self, item_id: int) -> Optional[Tuple[int, int]]:
        """Get item dimensions from metadata."""
        try:
            items = await self.get_items_by_ids([item_id])
            if items:
                return items[0].dimensions
        except Exception:
            pass
        return None

    # ── Version Control ───────────────────────────────────────────────────────

    async def check_out_items(self, item_ids: List[int]) -> bool:
        """Check out items for editing."""
        if not item_ids:
            return True
        request = DaminionVersionIdsRequest(ids=item_ids)
        try:
            response = await self._post("/api/VersionControl/CheckOut", request.to_dict())
            return self._is_version_control_accepted(response, item_ids)
        except Exception as e:
            logger.error(f"CheckOut failed: {e}")
            return False

    async def undo_check_out_items(self, item_ids: List[int]) -> bool:
        """Undo a checkout."""
        if not item_ids:
            return True
        request = DaminionVersionIdsRequest(ids=item_ids)
        try:
            response = await self._post("/api/VersionControl/UndoCheckOut", request.to_dict())
            return self._is_version_control_accepted(response, item_ids)
        except Exception as e:
            logger.warning(f"UndoCheckOut failed: {e}")
            return False

    async def check_in_item(
        self,
        item_id: int,
        file_path: str,
        message: Optional[str] = None,
        upload_file_name: Optional[str] = None,
    ) -> bool:
        """Check in a new file version (multipart)."""
        import aiofiles
        from pathlib import Path

        path = Path(file_path)
        if not path.exists():
            logger.error(f"CheckIn: file not found: {file_path}")
            return False

        part_file_name = upload_file_name or path.name

        async def send_checkin(comment: Optional[str] = None):
            if not self._client:
                raise DaminionNetworkError("Client not connected")
            async with aiofiles.open(path, "rb") as f:
                content = await f.read()

            files = {"file": (part_file_name, content, "application/octet-stream")}
            data = {"id": str(item_id)}
            if comment:
                data["comment"] = comment

            response = await self._client.post(
                "/api/VersionControl/CheckIn",
                data=data,
                files=files,
                timeout=None,
            )
            return response

        try:
            # Try with comment first
            if message:
                resp = await send_checkin(message)
                if resp.is_success and self._is_version_control_accepted(resp.json(), [item_id]):
                    return True
                logger.warning(f"CheckIn with comment rejected, retrying without")

            # Retry without comment
            resp = await send_checkin(None)
            if resp.is_success and self._is_version_control_accepted(resp.json(), [item_id]):
                return True

            logger.error(f"CheckIn refused for item {item_id}")
            return False
        except Exception as e:
            logger.error(f"CheckIn failed: {e}")
            return False

    @staticmethod
    def _is_version_control_accepted(body: Any, requested_ids: List[int]) -> bool:
        """Check if version control response indicates success."""
        if not body:
            return True
        if isinstance(body, bool):
            return body
        if isinstance(body, dict):
            if body.get("success") is False:
                return False
            if body.get("errorCode", 0) != 0:
                return False
            if "data" in body and isinstance(body["data"], dict):
                for id_val in requested_ids:
                    status = body["data"].get(str(id_val))
                    if status is False or (isinstance(status, int) and status < 0):
                        return False
        return True

    # ── Catalog Info ──────────────────────────────────────────────────────────

    async def get_catalog_guid(self) -> Optional[str]:
        """Get the catalog GUID for this session."""
        try:
            json_data = await self._get("/api/Settings/GetCatalogGuid")
            return self._extract_catalog_guid(json_data)
        except Exception as e:
            logger.warning(f"Catalog GUID lookup failed: {e}")
            return None

    @staticmethod
    def _extract_catalog_guid(json_data: Any) -> Optional[str]:
        """Extract catalog GUID from various response shapes."""
        if isinstance(json_data, str):
            return json_data.strip() or None
        if isinstance(json_data, dict):
            for key in ["guid", "Guid", "catalogGuid", "CatalogGuid", "catalogGUID", "value", "Value", "result", "Result"]:
                if key in json_data and json_data[key]:
                    return str(json_data[key]).strip()
            for key in ["data", "Data"]:
                if key in json_data:
                    nested = DaminionAPI._extract_catalog_guid(json_data[key])
                    if nested:
                        return nested
        return None


# ── Factory Function ──────────────────────────────────────────────────────────

async def create_daminion_client(
    server_url: str,
    username: str,
    password: str,
    catalog_id: Optional[str] = None,
) -> DaminionAPI:
    """Create and authenticate a DaminionAPI client from environment or args."""
    client = DaminionAPI(server_url, username, password)
    await client.connect()
    return client