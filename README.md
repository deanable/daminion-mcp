# Daminion MCP Server

A Model Context Protocol (MCP) server that exposes Daminion DAM API capabilities as tools for AI assistants.

## Features

Exposes 9 core tools across 4 categories:

### Media Search & Discovery
- **`search_media_items`** — Search catalog with free-text or structured queries, supports saved searches and collections
- **`get_media_item_details`** — Retrieve full metadata for specific items

### Taxonomies & Tag Management
- **`get_tag_schema`** — Dynamic tag ID/GUID mapping (no hardcoded IDs)
- **`get_tag_values`** — Fetch values for indexed tags (Keywords, Categories, etc.)
- **`batch_update_tags`** — Bulk assign/remove tags across multiple items

### Collections & Saved Searches
- **`list_shared_collections`** — List all shared collections with access codes
- **`get_shared_collection_items`** — Get items in a collection (with fallback handling)
- **`list_saved_searches`** — List saved searches with counts (bonus tool)

### Export, CDN & Downloads
- **`get_export_presets`** — List watermark/resize/format presets
- **`create_embed_link`** — Generate CDN links with optional preset

### Utility
- **`count_filtered_items`** — Count items matching filters (for pickers)

## Installation

```bash
cd mcp
pip install -r requirements.txt
```

## Configuration

Set these environment variables:

| Variable | Required | Description |
|----------|----------|-------------|
| `DAM_SERVER_URL` | Yes | Daminion server URL (e.g., `https://dam.example.com` or `dam.example.com:8080`) |
| `DAM_USERNAME` | Yes | Daminion username |
| `DAM_PASSWORD` | Yes | Daminion password |
| `DAM_CATALOG_ID` | No | Catalog ID (not used by API but reserved for future) |

## Running the Server

### Direct execution (stdio transport)
```bash
python -m src.mcp_server
```

### With environment variables
```bash
DAM_SERVER_URL=https://dam.example.com \
DAM_USERNAME=admin \
DAM_PASSWORD=secret \
python -m src.mcp_server
```

## Claude Desktop Configuration

Add to your `claude_desktop_config.json`:

### Windows
```json
{
  "mcpServers": {
    "daminion": {
      "command": "python",
      "args": ["C:\\Users\\Dean\\source\\repos\\Synapic.NET\\mcp\\src\\mcp_server.py"],
      "env": {
        "DAM_SERVER_URL": "https://your-server.daminion.net",
        "DAM_USERNAME": "admin",
        "DAM_PASSWORD": "your_password"
      }
    }
  }
}
```

### macOS/Linux
```json
{
  "mcpServers": {
    "daminion": {
      "command": "python3",
      "args": ["/path/to/Synapic.NET/mcp/src/mcp_server.py"],
      "env": {
        "DAM_SERVER_URL": "https://your-server.daminion.net",
        "DAM_USERNAME": "admin",
        "DAM_PASSWORD": "your_password"
      }
    }
  }
}
```

### Using a `.env` file (recommended)
Create `mcp/.env`:
```bash
DAM_SERVER_URL=https://your-server.daminion.net
DAM_USERNAME=admin
DAM_PASSWORD=your_password
```

Then in `claude_desktop_config.json`:
```json
{
  "mcpServers": {
    "daminion": {
      "command": "python",
      "args": ["C:\\Users\\Dean\\source\\repos\\Synapic.NET\\mcp\\src\\mcp_server.py"],
      "env": {
        "DAM_SERVER_URL": "${DAM_SERVER_URL}",
        "DAM_USERNAME": "${DAM_USERNAME}",
        "DAM_PASSWORD": "${DAM_PASSWORD}"
      }
    }
  }
}
```

## Tool Usage Examples

### Search for images by keyword
```json
{
  "tool": "search_media_items",
  "arguments": {
    "query": "landscape",
    "page_size": 20,
    "status_filter": "approved"
  }
}
```

### Get items from a saved search
```json
{
  "tool": "search_media_items",
  "arguments": {
    "scope": "saved_search",
    "saved_search_id": 42,
    "page_size": 50
  }
}
```

### Get full metadata for items
```json
{
  "tool": "get_media_item_details",
  "arguments": {
    "item_ids": [123, 456, 789],
    "get_all": true
  }
}
```

### List all shared collections
```json
{
  "tool": "list_shared_collections",
  "arguments": {}
}
```

### Get items in a collection
```json
{
  "tool": "get_shared_collection_items",
  "arguments": {
    "collection_id": 5,
    "page_size": 100
  }
}
```

### Add keywords to multiple items
```json
{
  "tool": "batch_update_tags",
  "arguments": {
    "item_ids": [100, 101, 102],
    "operations": [
      {"guid": "keywords-guid-from-schema", "value": "nature", "remove": false},
      {"guid": "keywords-guid-from-schema", "value": "outdoor", "remove": false}
    ]
  }
}
```

### Generate CDN link with export preset
```json
{
  "tool": "create_embed_link",
  "arguments": {
    "item_id": 12345,
    "preset_guid": "preset-guid-from-get_export_presets"
  }
}
```

## Error Handling

All tools return structured error responses:
```json
{
  "error": "Descriptive error message",
  "type": "DaminionAuthenticationError | DaminionRateLimitError | DaminionNotFoundError | DaminionNetworkError | InternalError"
}
```

Common errors:
- **DaminionAuthenticationError** — Invalid credentials or session expired
- **DaminionRateLimitError** — Too many requests, slow down
- **DaminionNotFoundError** — Item/collection/search not found
- **DaminionNetworkError** — Cannot reach server or network issue

## Architecture

```
mcp/
├── src/
│   ├── core/
│   │   └── daminion_api.py      # Core async client (port of C# DaminionApiClient)
│   └── mcp_server.py            # FastMCP server with 9 tool bindings
├── requirements.txt
└── README.md
```

The `DaminionAPI` client handles:
- Cookie-based authentication (`.AspNet.ApplicationCookie`)
- Automatic session recovery on 401/403
- Dynamic tag schema loading on startup
- Rate limiting
- URL encoding for special characters
- Robust fallbacks for collection/saved search endpoints

## Development

### Run with debug logging
```bash
DAM_SERVER_URL=... DAM_USERNAME=... DAM_PASSWORD=... \
LOG_LEVEL=DEBUG python -m src.mcp_server
```

### Test individual tools
```python
import asyncio
from src.core.daminion_api import create_daminion_client

async def test():
    client = await create_daminion_client(
        server_url="https://dam.example.com",
        username="admin",
        password="secret"
    )
    items = await client.get_items_page(scope="all", max_items=5)
    print(f"Found {len(items.items)} items")

asyncio.run(test())
```

## License

Internal use — Synapic.NET project.