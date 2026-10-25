# notion-mcp

An [MCP](https://modelcontextprotocol.io) server for the Notion API:
pages, databases (2025-09+ data-source model handled transparently),
native markdown read/write, comments, users, and a **full file
pipeline** — upload/replace/import/download with positional inserts,
page icons/covers, multi-part upload for large files, and in-place PDF
reading with optional OCR. Built on the
[Python MCP SDK](https://github.com/modelcontextprotocol/python-sdk)
(FastMCP); runs as a local stdio server or a containerized Streamable
HTTP service with bearer auth. 55 tools.

> Unofficial project, not affiliated with or endorsed by Notion.

## Table of Contents

- [Quick Start](#quick-start)
- [Tool Reference](#tool-reference)
  - [Search & Discovery](#search--discovery)
  - [Reading Pages](#reading-pages)
  - [Creating & Writing Pages](#creating--writing-pages)
  - [Editing Page Content](#editing-page-content)
  - [Page Lifecycle](#page-lifecycle)
  - [Block Operations](#block-operations)
  - [Database Schema](#database-schema)
  - [Database Queries](#database-queries)
  - [Database Rows & Files](#database-rows--files)
  - [File Downloads](#file-downloads)
  - [PDF Reading & OCR](#pdf-reading--ocr)
  - [Comments](#comments)
  - [Views](#views)
  - [Users](#users)
  - [Meeting Notes](#meeting-notes)
  - [Custom Emojis & Upload Listing](#custom-emojis--upload-listing)
- [Markdown Format](#markdown-format)
- [Working with Databases](#working-with-databases)
- [Working with Files](#working-with-files)
- [Common Workflows](#common-workflows)
- [Configuration](#configuration)
- [Architecture](#architecture)
- [Development](#development)
- [Testing](#testing)
- [Local stdio Mode](#local-stdio-mode)
- [Container Deployment](#container-deployment)
- [MCP Client Registration](#mcp-client-registration)
- [Notion API Version Notes](#notion-api-version-notes)

## Quick Start

```bash
# Prerequisites: Python 3.12+, uv
uv sync

# Set your Notion integration token
export NOTION_TOKEN=ntn_...

# Run the server
uv run notion-mcp
# Server starts on http://0.0.0.0:8322/mcp
```

Create a [Notion internal integration](https://www.notion.so/my-integrations) and share the pages/databases you want to access with it.

---

## Tool Reference

### Search & Discovery

#### `notion_search_pages`

Search for pages the integration can access.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `query` | string | yes | | Text to search for |
| `limit` | int | no | 10 | Max results (1-100) |
| `in_trash` | bool | no | `false` | Search trashed pages instead of active ones (2026-07 API addition; restore hits with `notion_restore_page`) |

Returns page titles, IDs, and URLs. Use the page ID with any tool that accepts `page_id`.

> Notion API: [POST /search](https://developers.notion.com/reference/post-search) with `filter.value = "page"`

#### `notion_search_databases`

Search for databases the integration can access. Returns schema summaries.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `query` | string | yes | | Text to search for |
| `limit` | int | no | 10 | Max results |

Returns `database_id`, `data_source_id`, property names and types. Use the `database_id` with database tools.

> Notion API: [POST /search](https://developers.notion.com/reference/post-search) with `filter.value = "data_source"` (changed from `"database"` in API 2025-09-03)

#### `notion_list_files_on_page`

List all file/image/pdf/video/audio blocks on a page.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `page_id` | string | yes | The page to scan |

Returns block IDs, filenames, block types, and source types. Use the `block_id` with `notion_download_file`.

> Notion API: [GET /blocks/{id}/children](https://developers.notion.com/reference/get-block-children), filtered to file-type blocks

#### `notion_list_local_files`

List files in either the server's local file store (`/data/files` inside the container) or the cross-MCP shared mount (`/shared`).

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `filter` | string | no | `""` | Substring filter on filename |
| `location` | string | no | `"files"` | `"files"` (the notion-mcp private store, `FILES_DIR`) or `"shared"` (the cross-MCP mount, `SHARED_DIR`, populated by `google-accounts-mcp download_attachment(destination='shared')`) |

#### `notion_purge_shared_files`

Delete files from the cross-MCP shared mount (`SHARED_DIR`, `/shared` in the container) whose modification time is older than `max_age_hours`. Pair with a host-side cron/timer TTL sweep if you want automatic cleanup; this tool is mainly for agents that want to clean up proactively at the end of a workflow.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `max_age_hours` | float | no | `24.0` | Minimum file age before deletion. Must be ≥ 0. |
| `dry_run` | bool | no | `False` | List victims without deleting |

Regular files only — subdirectories are left alone on principle.

---

### Reading Pages

There are two ways to read page content, optimized for different use cases.

#### `notion_read_page`

Read page content by fetching blocks and rendering them as markdown. Best for structured reading with optional block-level targeting.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `page_id` | string | yes | | The page to read |
| `max_depth` | int | no | 3 | Recursion depth for nested blocks (1-5) |
| `include_block_ids` | bool | no | false | Suffix each line with `<!-- block:UUID -->` |

When `include_block_ids` is true, each rendered line includes the block's UUID in an HTML comment. Use these IDs with `notion_update_block` or `notion_delete_block` for surgical edits.

> Notion API: [GET /blocks/{id}/children](https://developers.notion.com/reference/get-block-children) (recursive). Blocks are converted to markdown by `blocks.py`.

#### `notion_read_page_markdown`

Read page content using Notion's native markdown API. Faster (single API call) and returns Notion-flavored markdown including special tags for embedded databases, files, and page links.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `page_id` | string | yes | The page to read |

The output includes Notion-specific tags like `<database url="...">`, `<file src="...">`, and `<page url="...">` that aren't standard markdown but are useful for understanding page structure.

> Notion API: [GET /pages/{id}/markdown](https://developers.notion.com/reference/retrieve-page-markdown) (API 2025-09-03+)

**When to use which:**
- `notion_read_page` — when you need block IDs for editing, or want clean standard markdown
- `notion_read_page_markdown` — when you need speed, or want to see Notion-native structure (databases, synced blocks, etc.)

---

### Creating & Writing Pages

#### `notion_create_page`

Create a new page with optional markdown body content.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `title` | string | yes | | Page title |
| `content` | string | no | `""` | Markdown body content |
| `parent_page_id` | string | no | `""` | Create as child of this page |
| `database_id` | string | no | `""` | Create as row in this database |
| `properties_json` | string | no | `""` | JSON object of Notion property values |
| `icon` | string | no | `""` | Page icon — see [icon values](#icon-values) |
| `template` | string | no | `""` | Database rows: `"default"` or a template ID from `notion_list_templates`. The template supplies the body, so leave `content` empty |
| `position` | string | no | `""` | Where the page lands among a parent *page's* children: `"start"`, `"end"`, or a block ID to insert after |
| `silent` | bool | no | `false` | Skip the update, @mention and Person-property notifications this write would send |
| `native_markdown` | bool | no | `false` | Send `content` to Notion's own markdown parser instead of converting it to blocks locally (Notion-specific markup; very large pages run as a background task) |
| `markdown_version` | string | no | `""` | With `native_markdown`: `"v2"` opts into Notion-flavored Markdown (preview) |

Provide either `parent_page_id` (standalone page) or `database_id` (database row). The title property name is auto-detected from the database schema.

<a id="icon-values"></a>**Icon values** (`notion_create_page`, `notion_update_page`): an emoji (`"🚀"`), `"custom_emoji:<name-or-id>"` (names are resolved through `notion_list_custom_emojis`), `"icon:<name>[:<color>]"` for a native Notion icon (e.g. `"icon:pizza:blue"`), or an image URL. `notion_update_page` also takes `"remove"`.

Wiki page verification is an ordinary property: `{"Verification": {"verification": {"state": "verified"}}}` in `properties_json`.

With `native_markdown`, the request is sent with `allow_async`; the server waits for Notion's background task (up to ~150 s). If it is still running, the reply carries a task ID for `notion_get_async_task` — do not retry, or the page is created twice.

For database rows, pass additional properties via `properties_json` using [Notion property value format](https://developers.notion.com/reference/property-value-object):
```json
{
  "Status": {"select": {"name": "In Progress"}},
  "Priority": {"number": 5},
  "Due": {"date": {"start": "2026-03-15"}}
}
```

The `content` parameter accepts markdown which is parsed into Notion blocks (see [Markdown Format](#markdown-format)).

> Notion API: [POST /pages](https://developers.notion.com/reference/post-page) with `children` blocks, or `markdown` + `allow_async`; `template`, `position`, `notifications`

#### `notion_append_content`

Add markdown content to an existing page.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `page_id` | string | yes | | The page (or any block that supports children) to add to |
| `content` | string | yes | | Markdown text to add |
| `position` | string | no | `"end"` | `"end"`, `"start"`, or a block ID to insert directly after |
| `silent` | bool | no | `false` | Skip the notifications this write would send |

Content is parsed into blocks locally. Automatically batches in groups of 100 if the content produces more than 100 blocks (Notion API limit); later batches follow the first, so a positioned insert stays in order.

> Notion API: [PATCH /blocks/{id}/children](https://developers.notion.com/reference/patch-block-children)

#### `notion_insert_page_content`

Insert markdown using Notion's own parser — at the start, the end, or after a piece of existing text. Understands Notion-specific markup that `notion_append_content` does not.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `page_id` | string | yes | | The page to insert into |
| `markdown` | string | yes | | Content to insert |
| `position` | string | no | `"end"` | `"end"` or `"start"`; ignored when `after` is given |
| `after` | string | no | `""` | Insert after this passage, given as `"start text...end text"` |

> Notion API: [PATCH /pages/{id}/markdown](https://developers.notion.com/reference/update-page-markdown) with `type: "insert_content"`

#### `notion_add_tabs`

Add a tab block: a row of tabs, each with its own content.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `page_id` | string | yes | | The page (or child-bearing block) to add to |
| `tabs_json` | string | yes | | JSON array of `{"label", "icon"?, "content"?}` — `icon` is an emoji, `content` is markdown |
| `position` | string | no | `"end"` | `"end"`, `"start"`, or a block ID to insert after |

Tabs are created first and their content appended in a second pass, so tab content may itself be nested.

> Notion API: [Tab block](https://developers.notion.com/reference/block)

---

### Editing Page Content

#### `notion_update_page`

Update a page's title, icon, or database properties. Does not modify page body content.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `page_id` | string | yes | | The page to update |
| `title` | string | no | `""` | New title |
| `properties_json` | string | no | `""` | JSON of properties to update |
| `icon` | string | no | `""` | `"remove"` to clear, or any of the [icon values](#icon-values) |
| `silent` | bool | no | `false` | Skip the notifications this write would send |

For database row pages, properties follow the [Notion property value format](https://developers.notion.com/reference/property-value-object). Omitted properties are left unchanged.

> Notion API: [PATCH /pages/{id}](https://developers.notion.com/reference/patch-page)

#### `notion_update_page_content`

Edit page body content using search-and-replace on the page's markdown. Use `notion_read_page_markdown` first to see the exact content, then provide the text to find and replace.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `page_id` | string | yes | The page to edit |
| `old_text` | string | yes | Exact text to find |
| `new_text` | string | yes | Replacement text (empty string to delete) |
| `replace_all` | bool | no | If `old_text` occurs more than once the edit is rejected unless this is `true` |
| `allow_deleting_content` | bool | no | Must be `true` for an edit that would delete a child page or database |

> Notion API: [PATCH /pages/{id}/markdown](https://developers.notion.com/reference/update-page-markdown) with `type: "update_content"` (the deprecated `replace_content_range` was used before 2026-10)

#### `notion_replace_page_content`

Replace a page's entire body content with new markdown. Destructive — all existing content is overwritten.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `page_id` | string | yes | The page to overwrite |
| `markdown` | string | yes | New page content |
| `allow_deleting_content` | bool | no | Must be `true` when the replacement would delete a child page or database |
| `markdown_version` | string | no | `"v2"` treats `markdown` as Notion-flavored Markdown (preview) |

Sent with `allow_async`: a very large page is processed as a background task, which the server waits for (up to ~150 s). If it is still running the reply carries a task ID.

> Notion API: [PATCH /pages/{id}/markdown](https://developers.notion.com/reference/update-page-markdown) with `type: "replace_content"` (API 2025-09-03+)

#### `notion_get_async_task`

Check a background write that had not finished when the tool that started it returned.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `task_id` | string | yes | Task ID from that tool's reply |

> Notion API: [GET /async_tasks/{id}](https://developers.notion.com/reference/retrieve-async-task)

---

### Page Lifecycle

#### `notion_archive_page`

Archive (soft-delete) a page. It moves to the Notion trash and can be restored within 30 days.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `page_id` | string | yes | The page to archive |

> Notion API: [PATCH /pages/{id}](https://developers.notion.com/reference/patch-page) with `{"archived": true}`

#### `notion_restore_page`

Restore an archived page from the Notion trash.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `page_id` | string | yes | The archived page to restore |

> Notion API: [PATCH /pages/{id}](https://developers.notion.com/reference/patch-page) with `{"archived": false}`

#### `notion_move_page`

Move a page to a new parent page or into a database.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `page_id` | string | yes | | The page to move |
| `new_parent_page_id` | string | no | `""` | Move under this page |
| `new_parent_database_id` | string | no | `""` | Move into this database |

Provide exactly one of the parent parameters. When moving into a database, the server automatically resolves the `data_source_id`.

> Notion API: [POST /pages/{id}/move](https://developers.notion.com/reference/move-page) (API 2025-09-03+)

---

### Block Operations

Use `notion_read_page` with `include_block_ids=true` to get block IDs for these tools.

#### `notion_update_block`

Edit an existing block's text content in place.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `block_id` | string | yes | | The block to edit |
| `content` | string | yes | | New text (supports inline markdown) |
| `block_type` | string | no | auto-detect | Block type hint |

If `block_type` is omitted, the server fetches the block to detect its type. Supported types: `paragraph`, `heading_1`, `heading_2`, `heading_3`, `bulleted_list_item`, `numbered_list_item`, `to_do`, `quote`, `callout`, `toggle`, `code`.

> Notion API: [PATCH /blocks/{id}](https://developers.notion.com/reference/update-a-block)

#### `notion_delete_block`

Permanently delete a block and all its children.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `block_id` | string | yes | The block to delete |

> Notion API: [DELETE /blocks/{id}](https://developers.notion.com/reference/delete-a-block)

---

### Database Schema

#### `notion_describe_database`

Get a database's full property schema (column names and types).

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `database_id` | string | yes | The database to describe |

Returns the `database_id`, `data_source_id`, and all properties with their types. Use this to discover exact property names (case-sensitive) before querying or creating rows.

> Notion API: [GET /databases/{id}](https://developers.notion.com/reference/retrieve-a-database) + [GET /data_sources/{id}](https://developers.notion.com/reference/retrieve-a-data-source)

#### `notion_create_database`

Create a new database under a page — with your own schema, or one of Notion's typed databases.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `parent_page_id` | string | yes | | Page to create the database under |
| `title` | string | no | `""` | Database title (a typed database is otherwise named after its type) |
| `schema_json` | string | no | `""` | JSON property schema |
| `database_type` | string | no | `""` | `"tasks"`, `"projects"` or `"skills"`: Notion's built-in schema instead of `schema_json` |
| `inline` | bool | no | `false` | Show the database inline on the parent page |

Provide exactly one of `schema_json` and `database_type`.

The `schema_json` maps property names to [Notion property schema objects](https://developers.notion.com/reference/property-schema-object). A title property is required:

```json
{
  "Name": {"title": {}},
  "Status": {"select": {"options": [
    {"name": "To Do", "color": "red"},
    {"name": "In Progress", "color": "yellow"},
    {"name": "Done", "color": "green"}
  ]}},
  "Due Date": {"date": {}},
  "Priority": {"number": {"format": "number"}},
  "Tags": {"multi_select": {"options": [
    {"name": "bug", "color": "red"},
    {"name": "feature", "color": "blue"}
  ]}},
  "Assignee": {"people": {}},
  "Done": {"checkbox": {}},
  "Notes": {"rich_text": {}},
  "Link": {"url": {}}
}
```

> Notion API: [POST /databases](https://developers.notion.com/reference/create-a-database)

#### `notion_update_database`

Modify a database's title, description, or property schema.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `database_id` | string | yes | | The database to update |
| `title` | string | no | `""` | New title |
| `description` | string | no | `""` | New description |
| `properties_json` | string | no | `""` | JSON of property changes |

Property changes use [Notion's property schema format](https://developers.notion.com/reference/update-property-schema-object):

```json
// Add a column
{"New Column": {"rich_text": {}}}

// Remove a column
{"Old Column": null}

// Rename a column
{"Old Column": {"name": "New Name"}}

// Add select options
{"Status": {"select": {"options": [{"name": "Blocked", "color": "red"}]}}}
```

Multiple changes can be combined in one call.

> Notion API: [PATCH /data_sources/{id}](https://developers.notion.com/reference/update-a-data-source) (routed through data source in API 2025-09-03)

---

#### `notion_add_data_source`

Add another data source (a second table with its own schema) to an existing database. The other database tools operate on a database's first data source.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `database_id` | string | yes | The database to add to |
| `title` | string | yes | Name of the new data source |
| `schema_json` | string | yes | Property schema (same format as `notion_create_database`) |

> Notion API: [POST /data_sources](https://developers.notion.com/reference/create-a-data-source)

#### `notion_list_templates`

List the page templates defined on a database. Pass a template ID (or `"default"`) as `notion_create_page`'s `template`.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `database_id` | string | yes | The database to inspect |
| `name` | string | no | Case-insensitive substring filter |

> Notion API: [GET /data_sources/{id}/templates](https://developers.notion.com/reference/list-data-source-templates)

---

### Database Queries

#### `notion_query_database`

Query database rows with optional filter and sort.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `database_id` | string | yes | | The database to query |
| `filter_json` | string | no | `""` | [Notion filter object](https://developers.notion.com/reference/post-database-query-filter) as JSON |
| `sorts_json` | string | no | `""` | [Notion sort array](https://developers.notion.com/reference/post-database-query-sort) as JSON |
| `limit` | int | no | 50 | Page size (1-100 — Notion's hard cap) |
| `start_cursor` | string | no | `""` | Opaque cursor from a previous call. Pass it back to fetch the next page. |
| `archived` | bool | no | `false` | Return the database's archived rows instead of the active ones |
| `properties` | string | no | `""` | Comma-separated property names to include in the output (e.g. `"Gmail Message ID, Invoice, Status"`). The title column is always shown. Unknown names are ignored silently. Dramatically slims output when you only need a few columns. |

**Filter examples:**

```json
// Simple property filter
{"property": "Status", "select": {"equals": "Done"}}

// Number comparison
{"property": "Priority", "number": {"greater_than": 3}}

// Date filter
{"property": "Due", "date": {"before": "2026-04-01"}}

// Checkbox
{"property": "Done", "checkbox": {"equals": true}}

// Text contains
{"property": "Name", "title": {"contains": "meeting"}}

// Compound filter (AND)
{"and": [
  {"property": "Status", "select": {"equals": "Active"}},
  {"property": "Priority", "number": {"greater_than": 3}}
]}

// Compound filter (OR)
{"or": [
  {"property": "Status", "select": {"equals": "To Do"}},
  {"property": "Status", "select": {"equals": "In Progress"}}
]}
```

**Sort examples:**

```json
// Single sort
[{"property": "Due", "direction": "ascending"}]

// Multiple sorts
[
  {"property": "Priority", "direction": "descending"},
  {"property": "Name", "direction": "ascending"}
]

// Sort by timestamp
[{"timestamp": "last_edited_time", "direction": "descending"}]
```

Results are returned one page at a time. Each response ends with a
`has_more: <bool>  next_cursor: <token-or-(end)>` line; when
`has_more` is true, pass the `next_cursor` value back as `start_cursor`
on the next call to walk the next page. This gives the LLM bounded-
context pagination — walk a 10 000-row database 100 rows at a time
without ever materialising the whole thing in the tool output.

When `properties` is set, only the listed columns are included in the
per-row output (the title column is always shown). This is the fix
for "the query returns the full schema for every row and blows the
context budget" — set `properties="Gmail Message ID,Invoice"` and
the response shrinks to just those columns per row.

> Notion API: [POST /data_sources/{id}/query](https://developers.notion.com/reference/query-a-data-source) (API 2025-09-03+; the server resolves `database_id` to `data_source_id` automatically)

#### `notion_get_property`

Retrieve a single property value with full pagination. Useful for large relation, rollup, or rich_text properties that get truncated in normal page responses.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `page_id` | string | yes | | The page (row) to read from |
| `property_name` | string | no | `""` | Human-readable property name |
| `property_id` | string | no | `""` | Notion property ID (takes precedence) |

> Notion API: [GET /pages/{id}/properties/{property_id}](https://developers.notion.com/reference/retrieve-a-page-property-item)

---

### Database Rows & Files

#### `notion_upload_file`

Upload a file and attach it to a page as a new block.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `source` | string | yes | | File source (see below) |
| `parent_page_id` | string | yes | | Page — or any block that supports children (toggle, column, callout, ...) — to attach the file to |
| `filename` | string | no | `""` | Override the source filename |
| `caption` | string | no | `""` | Caption shown below the file in Notion |
| `position` | string | no | `"end"` | Where the new block lands: `"end"`, `"start"`, or a block ID to insert directly after that block |
| `embed_html` | bool | no | `false` | Attach an `.html`/`.htm` file as an embed block that Notion renders in the page, instead of a downloadable file block |

The `source` parameter accepts four schemes:
- `local:<path>` — file from the server's local store (`/data/files`)
- `shared:<filename>` — file from the cross-MCP shared mount
  (`/shared`). Populated by `google-accounts-mcp download_attachment(destination='shared')`.
  Preferred path for gmail → notion attachment handoffs; no base64, no size limit.
- `drive:<name-or-id>` — file from the shared Google Drive folder
- `base64:<data>` — raw base64-encoded content (requires `filename`).
  Subject to MCP parameter size limits; prefer `shared:` or `drive:`
  for anything over ~20 KB.

The block type (image, pdf, video, audio, file) is auto-detected from the MIME type. Files over 20 MB are uploaded via Notion's multi-part protocol automatically (10 MiB parts + complete). The response includes the new block's ID and the `sha256` of the uploaded bytes.

> Notion API: [POST /file_uploads](https://developers.notion.com/reference/create-a-file-upload) (single-part or multi-part) + [PATCH /blocks/{id}/children](https://developers.notion.com/reference/patch-block-children) (with `position`)

#### `notion_replace_file`

Replace the file inside an **existing** file/image/pdf/video/audio block, in place — the block keeps its position on the page. Use this to update a document without re-arranging anything.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `block_id` | string | yes | | The file block to update (from `notion_list_files_on_page`) |
| `source` | string | yes | | Same schemes as `notion_upload_file` |
| `filename` | string | no | `""` | Override the source filename |
| `caption` | string | no | `""` | Non-empty replaces the caption; empty leaves it untouched |

The new file must map to the same block type as the existing block (you cannot replace an image block's content with a PDF — delete and re-upload instead).

> Notion API: [PATCH /blocks/{id}](https://developers.notion.com/reference/update-a-block) with a `file_upload` reference

#### `notion_import_file_from_url`

Import a file into Notion directly from a public HTTPS URL — the bytes go Notion-side, never through this server or MCP parameters. The import is asynchronous; the tool polls until done or `wait_seconds` elapses.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `url` | string | yes | | Public HTTPS URL |
| `parent_page_id` | string | yes | | Page (or child-bearing block) to attach to |
| `filename` | string | yes | | Must carry an extension Notion accepts; determines block type |
| `caption` | string | no | `""` | Caption |
| `position` | string | no | `"end"` | As in `notion_upload_file` |
| `wait_seconds` | int | no | `60` | Max time to poll for the async import |

Size limits are plan-dependent (5 MiB free / 5 GiB paid workspaces).

> Notion API: [POST /file_uploads](https://developers.notion.com/reference/create-a-file-upload) with `mode=external_url` + [GET /file_uploads/{id}](https://developers.notion.com/reference/retrieve-a-file-upload) polling

#### `notion_set_page_visual`

Set or remove a page's icon or cover image.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `page_id` | string | yes | | Target page |
| `target` | string | yes | | `"icon"` or `"cover"` |
| `source` | string | no | `""` | An image via the usual schemes |
| `emoji` | string | no | `""` | Single emoji character (icon only) |
| `filename` | string | no | `""` | Override for `source` |
| `remove` | bool | no | `false` | Clear the icon/cover entirely |

Provide exactly one of `source`, `emoji`, or `remove=True`. Covers/icons from `source` must be images.

> Notion API: [PATCH /pages/{id}](https://developers.notion.com/reference/patch-page) with `icon` / `cover`

#### `notion_upload_file_to_database`

Create a new database row with a file attached.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `database_id` | string | yes | | Target database |
| `source` | string | yes | | File source (same schemes as above) |
| `files_property` | string | yes | | Exact name of the files column (case-sensitive) |
| `title` | string | yes | | Title for the new row |
| `title_property` | string | no | auto-detect | Override the title column name |
| `filename` | string | no | `""` | Override the source filename |

Use `notion_describe_database` first to find the exact `files_property` name.

> Notion API: [POST /file_uploads](https://developers.notion.com/reference/create-a-file-upload) + [POST /pages](https://developers.notion.com/reference/post-page)

#### `notion_add_file_to_row`

Add a file to an existing database row's files column.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `page_id` | string | yes | | The row (page) ID |
| `source` | string | yes | | File source |
| `files_property` | string | yes | | Exact name of the files column |
| `mode` | string | no | `"append"` | `"append"` or `"replace"` |
| `filename` | string | no | `""` | Override the source filename |

> Notion API: [POST /file_uploads](https://developers.notion.com/reference/create-a-file-upload) + [PATCH /pages/{id}](https://developers.notion.com/reference/patch-page)

#### `notion_batch_add_file_to_row`

Attach files to many existing rows in one call.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `items_json` | string | yes | | JSON array of item objects (see below) |
| `default_files_property` | string | no | `""` | Default `files_property` for items that omit it |
| `default_mode` | string | no | `"append"` | Default `mode` for items that omit it |

Each item in `items_json` takes the same fields as the single-shot
tool: `page_id`, `source`, optional `filename`, optional
`files_property`, optional `mode`. Defaults fill in any field left
off at the item level.

```json
[
  {"page_id": "p1", "source": "shared:m1_invoice.pdf"},
  {"page_id": "p2", "source": "shared:m2_invoice.pdf", "filename": "Renamed.pdf"},
  {"page_id": "p3", "source": "drive:<file_id>",
   "files_property": "Contracts", "mode": "replace"}
]
```

Items are processed with bounded concurrency — Notion's file upload API
is not pipelined and rejects rapid parallel stage-2 calls. Per-item
failures are reported in the output summary (`[i] FAIL page=... : ...`)
but do not abort the batch, so you can retry individual failing
items without re-running the whole set.

Typical pattern: stage N attachments via
`google-accounts-mcp/download_attachment(destination='shared', prefix=f"{msg}_")`,
call this tool once, then call `notion_purge_file` or
`notion_purge_shared_files` to free the shared slots.

> Notion API: same as `notion_add_file_to_row`, called N times under the hood.

#### `notion_purge_file`

Delete a single file from the cross-MCP shared mount by bare filename.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `filename` | string | yes | Bare filename (no path components) |

Use after a successful upload to free the shared slot instead of
waiting for the daily TTL sweep (or calling `notion_purge_shared_files`
with `max_age_hours=0`, which would wipe any unrelated in-flight
handoffs). Rejects any filename containing path separators.

### File Downloads

#### `notion_download_file`

Download a file from a Notion file/image/pdf/video/audio block.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `block_id` | string | yes | | File block ID (from `notion_list_files_on_page`) |
| `destination` | string | no | `"local"` | `"local"`, `"shared"`, `"drive"`, or `"base64"` |
| `filename` | string | no | `""` | Override the inferred filename (bare filename only for `shared`) |

Destinations:
- `local` — saves to the server's local file store
- `shared` — saves to the cross-MCP mount (`/shared`), immediately
  attachable by google-accounts-mcp as `attachments=['shared:<name>']` on
  `draft_email`/`send_email`. Collisions auto-rename atomically; 24h TTL.
- `drive` — uploads to the shared Google Drive folder
- `base64` — returns the file content as base64 in the response

Every destination reports the `sha256` of the downloaded bytes for
end-to-end integrity verification.

> Notion API: [GET /blocks/{id}](https://developers.notion.com/reference/retrieve-a-block) to get the signed S3 URL, then direct download

---

### PDF Reading & OCR

Read PDFs in place — no download hop needed. Both tools accept any file
source: `notion:<block_id>` (a file/pdf block on a page), `shared:<name>`,
`local:<path>`, or `drive:<name-or-id>`. Semantics are identical to
google-accounts-mcp's PDF tools (the `pdf_read` module is duplicated verbatim across
the two repos — edit both copies together).

#### `notion_extract_file_text`

Extract text from a PDF, with OCR for scanned/image-only pages.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `source` | string | yes | | `notion:<block_id>`, `shared:<name>`, `local:<path>`, or `drive:<name-or-id>` |
| `pages` | string | no | all | `pdftotext`-style spec: `"3"`, `"1-5"`, `"1,3,5"`, `"1-3,7"` |
| `mode` | string | no | `"text"` | `text` (flowing), `layout` (preserve columns), `tables` (markdown tables). Native extraction only |
| `ocr` | string | no | `"auto"` | `auto` — OCR pages with < 20 chars of native text; `off`; `force` — OCR every page; `llm` — vision model. OCR is delegated to an [xberg](https://github.com/xberg-io/xberg) server (`XBERG_BASE_URL`, default `http://xberg:8000`; classical backend `tesseract`; `llm` = xberg vlm backend → an OpenAI-compatible vision endpoint, needs `XBERG_VLM_API_KEY`) |

Returns JSON `{text, page_count, pages_returned, mode, truncated, filename,
source}` plus `ocr_used`/`ocr_pages`/`ocr_engine` when OCR ran (and
`ocr_error` if auto-mode OCR was needed but the local stack failed —
native text still returns). Response ceiling 200 KB, truncated on a page
boundary with a marker naming the pages to fetch next.

#### `notion_render_file_page`

Render one PDF page as a **viewable MCP image content block** — the
calling model sees the page directly (scans, charts, stamps — often no
OCR needed).

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `source` | string | yes | | Same schemes as `notion_extract_file_text` |
| `page` | int | no | `1` | 1-indexed page number |
| `max_width` | int | no | `1200` | Target pixel width, 200..4000 |
| `format` | string | no | `"jpeg"` | `jpeg` or `png` |
| `quality` | int | no | `75` | JPEG quality 1..100 |
| `return_base64` | bool | no | `false` | `true` returns JSON `{image_base64, mime_type, width, height, page, page_count, filename}` instead of an image block |

OCR is delegated to an [xberg](https://github.com/xberg-io/xberg)
server — no OCR wheels or OS libs needed here. See google-accounts-mcp's README
§ PDF OCR for the full tier/env table (shared `pdf_read` module).

---

### Comments

#### `notion_get_comments`

List all comments on a page.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `page_id` | string | yes | The page to read comments from |

Returns comment text, author name, timestamp, and `discussion_id` for threading.

> Notion API: [GET /comments](https://developers.notion.com/reference/retrieve-a-comment) with `block_id` parameter

#### `notion_get_comment`

Get a single comment by ID (text, author, timestamps, discussion thread).

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `comment_id` | string | yes | From `notion_get_comments` or `notion_add_comment` |

> Notion API: [GET /comments/{id}](https://developers.notion.com/reference/retrieve-comment)

#### `notion_add_comment`

Add a comment to a page, or reply to an existing discussion thread.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `page_id` | string | yes | | The page to comment on |
| `text` | string | yes | | Comment text (supports inline markdown) |
| `discussion_id` | string | no | `""` | Reply to this thread (from `notion_get_comments`) |

`text` is sent as native API markdown (2026-04 addition): inline
formatting, inline equations, and @mentions all work.

> Notion API: [POST /comments](https://developers.notion.com/reference/create-a-comment)

#### `notion_update_comment`

Edit a comment this integration created (the API returns 404 for
comments created by anyone else).

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `comment_id` | string | yes | From `notion_get_comments` / `notion_add_comment` |
| `text` | string | yes | Replacement text (native API markdown) |

> Notion API: [PATCH /comments/{id}](https://developers.notion.com/reference/update-a-comment)

#### `notion_delete_comment`

Delete a comment this integration created (404 for anyone else's).

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `comment_id` | string | yes | From `notion_get_comments` / `notion_add_comment` |

> Notion API: [DELETE /comments/{id}](https://developers.notion.com/reference/delete-a-comment)

---

### Views

The Views API (2026-03) exposes the saved views you see as tabs on a
database — each carries its own filter, sorts, and layout.

#### `notion_list_views`

List a database's views: name, type, id, and whether each carries a
saved filter/sorts. (The API's list endpoint returns identity-only
objects, so each view is hydrated with an extra retrieve.)

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `database_id` | string | yes | The database whose views to list |

#### `notion_query_view`

Run a view's **saved** filter and sorts — no filter JSON needed. Use
this instead of `notion_query_database` when a view already encodes the
question ("Open bugs", "This week").

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `view_id` | string | yes | | From `notion_list_views` |
| `limit` | int | no | `20` | Max rows (≤ 100) |

Implementation note: the API's view queries are cached objects with a
~15-minute TTL; the client creates one, reads a page, and best-effort
deletes it.

#### `notion_create_view` / `notion_update_view` / `notion_delete_view`

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `database_id` | string | create | | Parent database |
| `view_id` | string | update/delete | | Target view |
| `name` | string | create | `""` (update) | Display name |
| `view_type` | string | no | `"table"` | table / board / list / calendar / timeline / gallery / chart |
| `filter_json` | string | no | `""` | Notion filter object (same shape as `notion_query_database`) |
| `sorts_json` | string | no | `""` | JSON array of sort objects |

Gotcha (verified live): view creation requires **both** `database_id`
and `data_source_id` — the client resolves the data source through the
same cache the query path uses. Deleting a view never touches rows.

---

### Users

#### `notion_list_users`

List all workspace users (people and bots).

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `name_contains` | string | no | Case-insensitive filter on the user's name |

Returns names, IDs, emails, and types. User IDs are needed for `people` properties and `@mentions` in comments.

> Notion API: [GET /users](https://developers.notion.com/reference/get-users) (paginated)

#### `notion_get_user`

Get details for a specific user.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `user_id` | string | yes | Notion user UUID, or `"me"` for this integration's own bot user |

Returns name, email, avatar URL, type (person/bot), and owner info for bots. `"me"` also shows the connected workspace and its file-upload size limit.

> Notion API: [GET /users/{id}](https://developers.notion.com/reference/get-user), [GET /users/me](https://developers.notion.com/reference/get-self)

---

### Meeting Notes

Notion AI meeting notes. They need AI meeting notes on the integration's workspace; without it the API returns 400.

#### `notion_query_meeting_notes`

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `title_contains` | string | no | `""` | Simple title filter |
| `filter_json` | string | no | `""` | Full meeting-notes filter; overrides `title_contains` |
| `sort_json` | string | no | `""` | Sort array (default: newest first) |
| `limit` | int | no | 10 | 1-50; the API has no paging here |

> Notion API: [POST /blocks/meeting_notes/query](https://developers.notion.com/reference/query-meeting-notes)

#### `notion_read_meeting_notes`

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `block_id` | string | yes | | Meeting notes block ID |
| `section` | string | no | `"summary"` | `"summary"`, `"notes"` or `"transcript"` |

#### `notion_create_meeting_notes`

Create a transcript and summary from a recording. **Not idempotent** — a second call creates a second set of notes, so the server never retries it. Processing continues after the call returns.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `source` | string | yes | | An audio/video file (`shared:`, `local:`, `drive:`) or `notion:<block_id>` for an audio block already on a page |
| `parent_page_id` | string | for files | `""` | Page the notes are added to |
| `title` | string | no | `""` | Meeting title |
| `language` | string | no | `""` | Transcription hint, e.g. `"en"` |
| `summarize` | bool | no | `true` | `false` transcribes without a summary |

> Notion API: [POST /blocks/meeting_notes](https://developers.notion.com/reference/create-meeting-note)

---

### Custom Emojis & Upload Listing

#### `notion_list_custom_emojis`

List workspace custom emojis; optional exact `name` filter. Use one as an icon with `"custom_emoji:<name>"`.

> Notion API: [GET /custom_emojis](https://developers.notion.com/reference/list-custom-emojis)

#### `notion_list_file_uploads`

List files this integration has uploaded, with status. `status` filters to `pending`, `uploaded`, `expired` or `failed`; `limit` 1-100.

> Notion API: [GET /file_uploads](https://developers.notion.com/reference/list-file-uploads)

---

## Markdown Format

### Writing (markdown to blocks)

When you pass `content` to `notion_create_page` or `notion_append_content`, the markdown is parsed into Notion blocks. Supported syntax:

| Markdown | Notion Block |
|----------|-------------|
| `# Heading` | `heading_1` |
| `## Heading` | `heading_2` |
| `### Heading` | `heading_3` |
| `#### Heading` | `heading_4` (2026-03 API addition; deeper levels clamp to it) |
| `Plain text` | `paragraph` |
| `- Item` or `* Item` | `bulleted_list_item` |
| `1. Item` | `numbered_list_item` |
| `- [x] Done` | `to_do` (checked) |
| `- [ ] Open` | `to_do` (unchecked) |
| `> Quote` | `quote` (consecutive `>` lines merge) |
| `` ```python ... ``` `` | `code` (with language) |
| `---` or `***` or `___` | `divider` |

Inline formatting within any block:

| Markdown | Rendering |
|----------|-----------|
| `**bold**` | Bold |
| `*italic*` | Italic |
| `***both***` | Bold + Italic |
| `` `code` `` | Inline code |
| `~~strike~~` | Strikethrough |
| `[text](url)` | Link |

### Reading (blocks to markdown)

`notion_read_page` renders all common Notion block types:

- Paragraphs, headings (1-3), bullet/numbered lists, to-do items
- Quotes, callouts (with emoji icons), toggles
- Code blocks (with language), dividers, equations
- Images (`![caption](url)`), files, bookmarks, embeds
- Tables (rendered as markdown tables)
- Child pages and databases (shown as `[Page: Title]` / `[Database: Title]`)
- Tab blocks (each tab as `**[Tab: label]**` followed by its content)
- Meeting notes (shown as `[Meeting notes: Title] (status)` with the block ID for `notion_read_meeting_notes`)
- Nested blocks (indented, recursive up to `max_depth`)

---

## Working with Databases

### API 2025-09-03 Data Source Model

In the 2025-09-03 API, database properties and queries operate through **data sources** rather than directly on databases. This server handles the mapping transparently:

1. You pass a `database_id` (from search results or a Notion URL)
2. The server calls `GET /databases/{id}` to find the `data_source_id`
3. Schema/query/update operations go through `/data_sources/{data_source_id}`
4. The mapping is cached per session for performance

You never need to know or pass data source IDs directly.

### Typical Database Workflow

```
1. notion_search_databases("project")     → find the database ID
2. notion_describe_database(db_id)        → see property names and types
3. notion_query_database(db_id, filter)   → read rows
4. notion_create_page(database_id=db_id)  → add a row
5. notion_update_page(row_id, props)      → update a row's properties
```

### Property Types Reference

The [Notion property value format](https://developers.notion.com/reference/property-value-object) for common types:

```json
// Title (every database has exactly one)
{"Title Column": {"title": [{"text": {"content": "My Title"}}]}}

// Select
{"Status": {"select": {"name": "Done"}}}

// Multi-select
{"Tags": {"multi_select": [{"name": "bug"}, {"name": "urgent"}]}}

// Number
{"Priority": {"number": 5}}

// Date (single)
{"Due": {"date": {"start": "2026-03-15"}}}

// Date (range)
{"Sprint": {"date": {"start": "2026-03-01", "end": "2026-03-15"}}}

// Checkbox
{"Done": {"checkbox": true}}

// URL
{"Link": {"url": "https://example.com"}}

// Email
{"Contact": {"email": "user@example.com"}}

// Rich text
{"Notes": {"rich_text": [{"text": {"content": "Some notes"}}]}}

// People (requires user IDs from notion_list_users)
{"Assignee": {"people": [{"id": "user-uuid-here"}]}}

// Relation (requires page IDs)
{"Related": {"relation": [{"id": "page-uuid-here"}]}}
```

---

## Working with Files

### File Source Schemes

All upload tools accept a `source` parameter with one of five schemes:

| Scheme | Format | Example | Notes |
|--------|--------|---------|-------|
| `notion` | `notion:<block_id>` | `notion:21f8a834-…` | A file/image/pdf/video/audio block already in the workspace (block IDs from `notion_list_files_on_page`). Lets the PDF tools read workspace files in place, and upload tools copy a file between pages. |
| `local` | `local:<path>` | `local:reports/q3.pdf` | Relative to `/data/files` in the container (`FILES_DIR`). List with `notion_list_local_files()`. |
| `shared` | `shared:<filename>` | `shared:invoice.pdf` | Relative to `/shared` in the container (`SHARED_DIR`), the cross-MCP mount shared with the companion `google-accounts-mcp` server. Populated by `google-accounts-mcp/download_attachment(destination='shared')`. List with `notion_list_local_files(location='shared')`. Preferred path for gmail → notion handoffs — no base64, no size limit. |
| `drive` | `drive:<name-or-id>` | `drive:budget.xlsx` | From the shared Google Drive folder |
| `base64` | `base64:<data>` | `base64:SGVsbG8=` | Requires `filename` parameter. Subject to MCP parameter size limits; prefer `shared:` or `drive:` for anything larger than ~20 KB. |

### File Upload Protocol

Notion uses a 3-stage upload protocol:
1. **Create** — `POST /file_uploads` returns an `upload_id`
2. **Send** — `POST /file_uploads/{id}/send` with multipart file data
3. **Reference** — attach the `upload_id` in a block or page property (must be used within 1 hour)

This is handled automatically by all upload tools.

### Cross-MCP File Handoff (`shared:` scheme)

**Shared-store invariants**: the mount is read/write from both MCP
servers. Filename clashes during `google-accounts-mcp download_attachment` are
resolved by auto-renaming (`invoice.pdf` → `invoice-2.pdf`), and the
return value always reports the actual saved name to reference. Files
are swept 24 hours after last modification by the
host-side TTL sweep (if you run one) — stage to Notion
within that window. For immediate cleanup after a workflow, call the
`notion_purge_shared_files` tool (see the "Files" tool table).

`notion-mcp` and `google-accounts-mcp` share a host volume
(`~/.local/share/containers/data/mcp-shared/`) mounted into both
containers at `/shared`. Files dropped there by
`google-accounts-mcp download_attachment(destination='shared')` are immediately
readable via the `shared:` source scheme:

```python
# From google-accounts-mcp:
download_attachment(message_id="...", filename="invoice.pdf",
                    destination="shared")
# From notion-mcp:
notion_add_file_to_row(page_id="...", source="shared:invoice.pdf",
                       files_property="Attachments")
```

The pipeline is **bidirectional** — the reverse direction stages a
Notion file for google-accounts-mcp to attach:

```python
# From notion-mcp:
notion_download_file(block_id="...", destination="shared")
# From google-accounts-mcp:
draft_email(to=["..."], subject="...", body="...",
            attachments=["shared:contract.pdf"])
```

The file bytes never traverse MCP tool parameters, so there is no size
limit and the 2026-04 "Energy Locals 1.57 MB invoice" pipeline failure
no longer applies. Use this path by default for file transfers between
the two servers in either direction. All transfer tools report `sha256`
so the agent can verify integrity at each hop ( `notion_list_local_files`
and gmail's `list_shared_files` include it per file too).

### Google Drive Integration

The Drive integration is **optional**. Without it, skip the Drive env vars — every other file source keeps working, and `drive:` sources return a clear setup error. Use `drive:` when you want the file to persist in Drive; use `shared:` when you just need a one-shot handoff between the two MCP servers.

Access tokens for `DRIVE_ACCOUNT` come from one of two places:

- **Broker mode** (`GOOGLE_AUTH_URL` set): a google-auth token broker hands out short-lived access tokens over HTTP (`GET /token?account=`, authenticated with `GOOGLE_AUTH_BEARER`). notion-mcp then holds no OAuth client secret and no refresh token, and a re-authorised account is picked up without a restart. Broker failures surface as actionable tool errors (re-authorise the account, account not shared with this service, missing scope, broker unreachable). The client is a single vendored stdlib file, `broker_client.py` — no extra dependency.
- **Local mode** (`GOOGLE_AUTH_URL` unset): reuse OAuth refresh tokens from [google-accounts-mcp](https://github.com/snickery/google-accounts-mcp)'s SQLite token DB (mounted read-only at `/google-data/tokens.db`) with the same Google client ID and secret. The refresh token is read once per process, so restart notion-mcp after re-authorising.

Either way the token must come from the same OAuth client google-accounts-mcp uses: the `drive.file` scope only sees files that client created.

---

## Common Workflows

### Read a page and make a targeted edit

```
1. notion_read_page_markdown(page_id)           → see current content
2. notion_update_page_content(page_id,
     old_text="Draft version",
     new_text="**Final** version")               → search-replace edit
```

### Create a meeting notes page

```
1. notion_create_page(
     title="Sprint Planning 2026-03-15",
     parent_page_id="meetings-page-id",
     icon="📋",
     content="# Agenda\n\n- [ ] Review backlog\n- [ ] Assign tasks\n\n# Notes\n\n")
```

### Query a database and update a row

```
1. notion_query_database(db_id,
     filter_json='{"property": "Status", "select": {"equals": "In Progress"}}')
2. notion_update_page(row_id,
     properties_json='{"Status": {"select": {"name": "Done"}}}')
```

### Move a page into a database

```
1. notion_move_page(page_id, new_parent_database_id=db_id)
```

### Download a file from Notion to Drive

```
1. notion_list_files_on_page(page_id)            → get block_id
2. notion_download_file(block_id, destination="drive")
```

### Email a file stored in Notion

```
1. notion_list_files_on_page(page_id)            → get block_id
2. notion_download_file(block_id, destination="shared")
3. google-accounts-mcp/draft_email(..., attachments=["shared:<filename>"])
```

### Edit a specific block on a page

```
1. notion_read_page(page_id, include_block_ids=true)  → find block UUID
2. notion_update_block(block_id, "New **content**")    → edit in place
```

---

## Configuration

All config is via environment variables. For container deployment, put secrets in `.secrets.env`:

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `NOTION_TOKEN` | Yes | | [Internal integration token](https://www.notion.so/my-integrations) (starts with `ntn_`) |
| `MCP_BEARER_TOKEN` | Yes | | Bearer token for authenticating MCP clients |
| `NOTION_API_VERSION` | No | `2026-03-11` | Notion API version header |
| `PORT` | No | `8322` | Server listen port |
| `FILES_DIR` | No | `/data/files` | Local file store path |
| `DRIVE_ACCOUNT` | For Drive | | Google account email for Drive access |
| `GOOGLE_AUTH_URL` | Drive, broker mode | | Base URL of a google-auth token broker (e.g. `http://google-auth:8335`). Setting it selects broker mode |
| `GOOGLE_AUTH_BEARER` | Drive, broker mode | | This service's bearer token for the broker |
| `GOOGLE_CLIENT_ID` | Drive, local mode | | OAuth client ID (same as google-accounts-mcp) |
| `GOOGLE_CLIENT_SECRET` | Drive, local mode | | OAuth client secret |
| `GOOGLE_TOKEN_DB_PATH` | Drive, local mode | `/google-data/tokens.db` | Path to google-accounts-mcp's SQLite token DB (read-only; the legacy `GMAIL_TOKEN_DB_PATH` name is still honored) |
| `DRIVE_FOLDER_NAME` | For Drive | `mcp-google-accounts` | Shared Drive folder name |

## Architecture

```
src/notion_mcp/
  server.py          # FastMCP tool definitions, HTTP server, bearer auth middleware
  notion_client.py   # Notion REST client — all API calls, pagination, caching
  blocks.py          # Bidirectional Notion blocks <-> markdown conversion
  drive_client.py    # Google Drive client (token broker, or google-accounts-mcp's token DB)
  broker_client.py   # google-auth token broker client (vendored verbatim, stdlib only)
  config.py          # Environment variable configuration
```

**Key design decisions:**
- `notion_client.py` handles all HTTP, pagination, and the database → data_source mapping. Tools in `server.py` never call the Notion API directly.
- `blocks.py` is a pure-function module with zero side effects — easy to test and reuse.
- Data source IDs are cached in memory per `NotionClient` instance to avoid redundant lookups.
- Block appends are auto-batched at 100 (Notion API limit).
- The bearer auth middleware is pure ASGI, compatible with streamed responses.
- **Threadpool tool offload** (`server.py`): the MCP SDK runs synchronous `@mcp.tool()` handlers *inline on the event loop*, so one blocking `httpx` call would freeze every other request (the cause of the "server unresponsive after a burst" stalls). `mcp.tool` is wrapped so each sync tool runs its body in a worker thread (`anyio.to_thread.run_sync`); the loop stays free. The wrapper preserves the tool signature (client schemas unchanged — no restart) and returns the original sync function as the module name (tool-to-tool calls and tests still work). Each call logs `tool=… outcome=… duration_ms=…` for Loki.
- **Rate-limit backoff** (`notion_client._request`): retries `429/502/503/504` up to 4 attempts honouring `Retry-After` (capped 30s/attempt). The backoff sleep runs in the offload thread, never blocking the loop. `send_file_upload` timeout is capped at 180s — below the MCP client's 240s ceiling — so the server fails before the client does.
- **Container healthcheck**: `python -m notion_mcp.healthcheck` does a full HTTP round-trip to `/mcp`; a wedged loop fails the probe and `HealthOnFailure=kill` + `Restart=always` restart the container.

## Development

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
# Install all dependencies including test extras
uv sync --extra test

# Run the server locally
NOTION_TOKEN=ntn_... uv run notion-mcp

# Run unit tests (fast, no API calls needed)
uv run --extra test pytest tests/ --ignore=tests/test_integration.py -v

# Run all tests including live API
NOTION_TEST_TOKEN=ntn_... uv run --extra test pytest -v
```

## Testing

### Unit Tests (294 tests, ~2s)

No API access required. Seven test files (the PDF-tool tests mock the
xberg HTTP calls — no network):

**`test_blocks.py`** (83 tests) — markdown conversion:
- `rich_text_to_md` / `md_to_rich_text`: all inline formatting variants
- `blocks_to_markdown`: every block type, nesting, `include_block_ids`, tables
- `markdown_to_blocks`: headings, lists, quotes, code, dividers, mixed content
- `format_property_value`: all 20+ Notion property types
- `format_page_properties`: multi-property formatting

**`test_notion_client.py`** (95 tests) — client logic with mocked HTTP:
- Async-task polling, silent notifications, templates, typed databases, positioned appends, status-group beta header, body `retry_after`
- Pagination for `list_children`, `query_data_source`, `list_comments`, `list_users`
- `resolve_data_source_id` caching and error handling
- `create_page` child batching (>100 blocks)
- Request body construction for markdown, move, comment endpoints
- `pick_block_type` MIME mapping
- `find_title_property` / `get_page_title` edge cases

**`test_server_tools.py`** (69 tests) — tool-layer logic with a mocked client
(file source resolution, positional inserts, replace flows, purge tools, icon
values, tabs, meeting notes, HTML embeds).

**`test_sources.py`** (13 tests) — `_read_source` scheme parsing for
`local:`/`shared:`/`base64:` (the `drive:` scheme lives in `test_drive_client.py`).

**`test_purge.py`** (6 tests) — `notion_purge_shared_files` filters.

**`test_pdf_tools.py`** (16 tests) — `notion_extract_file_text` /
`notion_render_file_page` wiring, the `notion:` source scheme, image vs
base64 return modes, OCR plumbing (xberg calls mocked; full OCR-tier
semantics are covered in google-accounts-mcp's `test_pdf_read.py`, since
`pdf_read.py` is duplicated verbatim across the two repos).

**`test_drive_client.py`** (13 tests) — Drive client; guards the read-only
tokens-DB regression (sqlite must open with `immutable=1`) and covers broker
mode (credential refresh, cache bypass after a 401, error mapping).

```bash
uv run --extra test pytest tests/ --ignore=tests/test_integration.py -v
```

### Integration Tests (26 tests, ~70s)

Hit the live Notion API against a sandbox page you provide. Require:
- `NOTION_TEST_TOKEN` — an integration token with access to the sandbox
- `NOTION_TEST_PAGE_ID` — a scratch page the tests may write to
- `NOTION_TEST_DB_ID` — an inline database on that page

Tests create pages, exercise all endpoints, and clean up after themselves:
- Connectivity (search, list users, retrieve page)
- Page CRUD (create, read, archive, restore)
- Blocks (append, read, update, delete)
- Markdown API (read, replace, search-replace, insert)
- Data sources (resolve, get schema, query, search)
- Move page (create parent A/B, move child, verify)
- Comments (create, list)
- Views (lifecycle, query)
- 2026-10 additions: native-markdown create, markdown insert/update/replace, positioned and silent writes, tabs, HTML embeds, typed databases, second data sources, templates, archived and status-group queries, the small read endpoints

Not exercised live: `notion_create_meeting_notes` (it needs a real recording and spends Notion AI) and template-based page creation (the sandbox database has no templates).

```bash
NOTION_TEST_TOKEN=ntn_... uv run --extra test pytest tests/test_integration.py -v
```

Run integration tests after:
- Upgrading `NOTION_API_VERSION` in `config.py`
- Changing endpoint paths or request body formats
- Modifying pagination or error handling

## Local stdio Mode

`--stdio` starts FastMCP's stdio transport: no uvicorn, no bearer token
(the client owns the spawned process; there is no network surface). This
is what most interactive MCP clients should use:

```jsonc
// e.g. Claude Desktop claude_desktop_config.json / Claude Code .mcp.json
{
  "mcpServers": {
    "notion": {
      "command": "uv",
      "args": ["run", "--project", "/path/to/notion-mcp",
               "notion-mcp", "--stdio"],
      "env": {
        "NOTION_TOKEN": "ntn_...",
        "FILES_DIR": "/home/you/.local/share/notion-mcp/files"
      }
    }
  }
}
```

Prefer an env file over inline values where your client supports it
(`uv run --env-file ...`).

## Container Deployment (HTTP)

```bash
podman build -t notion-mcp .   # or: docker build -t notion-mcp .
podman run -d --name notion-mcp -p 8322:8322 -v notion-data:/data \
  -e NOTION_TOKEN=ntn_... \
  -e MCP_BEARER_TOKEN=some-long-random-token \
  notion-mcp
```

HTTP mode **refuses to start** without `MCP_BEARER_TOKEN`; clients
register with:

```json
{
  "mcpServers": {
    "notion": {
      "url": "https://your-host:8322/mcp",
      "headers": {"Authorization": "Bearer <MCP_BEARER_TOKEN>"}
    }
  }
}
```

`notion_mcp.healthcheck` does a full HTTP round-trip to `/mcp` (the 401
counts as alive); wire it to your container healthcheck. Terminate TLS at
a reverse proxy — the server itself speaks plain HTTP. For the optional
shared-mount feature, add the `/shared` mount shared with a
google-accounts-mcp container; for Drive in local mode, also mount its
token DB read-only at `/google-data` (broker mode needs no mount).

## Notion API Version Notes

This server uses Notion API **2026-03-11**. Version history:

### 2025-09-03 (from 2022-06-28)

| Feature | Old (2022-06-28) | New (2025-09-03) |
|---------|-------------------|-------------------|
| Database properties | On `/databases/{id}` response | Moved to `/data_sources/{id}` |
| Database query | `POST /databases/{id}/query` | `POST /data_sources/{id}/query` |
| Database search | `filter.value = "database"` | `filter.value = "data_source"` |
| Markdown read | Not available | `GET /pages/{id}/markdown` |
| Markdown write | Not available | `PATCH /pages/{id}/markdown` |
| Move pages | Not available | `POST /pages/{id}/move` |

### 2026-03-11 (from 2025-09-03)

| Feature | Old (2025-09-03) | New (2026-03-11) |
|---------|-------------------|-------------------|
| Trash status field | `"archived": true/false` | `"in_trash": true/false` |
| Block append positioning | `"after": "block-id"` | `"position": {"type": "after_block", "after_block": {"id": "..."}}` |
| Transcription block type | `"transcription"` | `"meeting_notes"` |

The `position` object also supports `{"type": "start"}` and `{"type": "end"}` for inserting at the beginning or end of a parent block.

### Additions within 2026-03-11

Notion keeps adding to this version without a new version string. Adopted so far:

| Added | Feature | Where |
|-------|---------|-------|
| 2026-03 | Views API, `heading_4`, tab blocks, custom emoji list, native icons | view tools, `notion_add_tabs`, `notion_list_custom_emojis`, icon values |
| 2026-04 | Comment update/delete, markdown comments | comment tools |
| 2026-05 | Markdown `insert_content.position`, meeting-notes query | `notion_insert_page_content`, `notion_query_meeting_notes` |
| 2026-06 | `allow_async` + async tasks, 529 retry | `native_markdown`, `notion_replace_page_content`, `notion_get_async_task` |
| 2026-07 | `filter.in_trash`, `is_archived`, HTML embeds | `notion_search_pages`, `notion_query_database(archived)`, `notion_upload_file(embed_html)` |
| 2026-08 | Create meeting notes | `notion_create_meeting_notes` |
| 2026-09 | `database_type`, body `retry_after`, `markdown_version` (preview) | `notion_create_database`, retries, markdown tools |
| 2026-10 | `notifications.mode: silent`, option recolouring, status-group filters (beta header sent automatically) | `silent`, `notion_update_database`, `notion_query_database` |

Not implemented by design: webhooks, OAuth token endpoints, the Admin API and the Notion Agent APIs (this server uses one internal integration token and has no event receiver), and `filter_properties` on page writes (it only trims a response the tools do not return).

The server handles the data source migration transparently — you always pass `database_id` and the server resolves the `data_source_id` internally (with caching).

If upgrading from an older version, run the integration test suite to verify nothing breaks:

```bash
NOTION_TEST_TOKEN=ntn_... uv run --extra test pytest tests/test_integration.py -v
```

## License

[MIT](LICENSE)
