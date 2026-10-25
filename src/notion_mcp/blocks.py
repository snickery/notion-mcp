"""Convert between Notion blocks and Markdown text.

Rendering (blocks -> markdown): handles all common block types, nested children.
Parsing (markdown -> blocks): handles headings, lists, quotes, code, dividers.
Property formatting: renders Notion property values as readable strings.
"""

import re

# ---------------------------------------------------------------------------
# Rich text <-> Markdown
# ---------------------------------------------------------------------------

def rich_text_to_md(rt_array: list[dict]) -> str:
    """Convert a Notion rich_text array to inline markdown."""
    parts = []
    for rt in rt_array:
        text = rt.get("plain_text", "")
        if not text:
            continue
        ann = rt.get("annotations", {})
        href = rt.get("href")

        if ann.get("code"):
            text = f"`{text}`"
        elif ann.get("bold") and ann.get("italic"):
            text = f"***{text}***"
        elif ann.get("bold"):
            text = f"**{text}**"
        elif ann.get("italic"):
            text = f"*{text}*"
        if ann.get("strikethrough"):
            text = f"~~{text}~~"
        if href and not ann.get("code"):
            text = f"[{text}]({href})"

        parts.append(text)
    return "".join(parts)


_INLINE_RE = re.compile(
    r"(\*\*\*(.+?)\*\*\*)"       # bold+italic
    r"|(\*\*(.+?)\*\*)"          # bold
    r"|(\*(.+?)\*)"              # italic
    r"|(`(.+?)`)"                # inline code
    r"|(~~(.+?)~~)"              # strikethrough
    r"|(\[([^\]]+)\]\(([^)]+)\))"  # link
)


def md_to_rich_text(text: str) -> list[dict]:
    """Parse inline markdown into Notion rich_text objects."""
    if not text:
        return []

    result: list[dict] = []
    pos = 0
    for m in _INLINE_RE.finditer(text):
        if m.start() > pos:
            result.append(_plain_rt(text[pos:m.start()]))

        if m.group(2):
            result.append(_annotated_rt(m.group(2), bold=True, italic=True))
        elif m.group(4):
            result.append(_annotated_rt(m.group(4), bold=True))
        elif m.group(6):
            result.append(_annotated_rt(m.group(6), italic=True))
        elif m.group(8):
            result.append(_annotated_rt(m.group(8), code=True))
        elif m.group(10):
            result.append(_annotated_rt(m.group(10), strikethrough=True))
        elif m.group(12):
            result.append(_link_rt(m.group(12), m.group(13)))
        pos = m.end()

    if pos < len(text):
        result.append(_plain_rt(text[pos:]))

    return result or [_plain_rt(text)]


def _plain_rt(text: str) -> dict:
    return {"type": "text", "text": {"content": text}}


def _annotated_rt(
    text: str,
    bold: bool = False,
    italic: bool = False,
    code: bool = False,
    strikethrough: bool = False,
) -> dict:
    annotations: dict = {}
    if bold:
        annotations["bold"] = True
    if italic:
        annotations["italic"] = True
    if code:
        annotations["code"] = True
    if strikethrough:
        annotations["strikethrough"] = True
    return {"type": "text", "text": {"content": text}, "annotations": annotations}


def _link_rt(text: str, url: str) -> dict:
    return {"type": "text", "text": {"content": text, "link": {"url": url}}}


# ---------------------------------------------------------------------------
# Blocks -> Markdown
# ---------------------------------------------------------------------------

def blocks_to_markdown(
    blocks: list[dict], indent: int = 0, include_ids: bool = False,
) -> str:
    """Render a list of Notion blocks as markdown text.

    Blocks with nested children should have a ``_children`` key containing the
    child block list (attached by ``NotionClient.get_block_children_deep``).

    When *include_ids* is True, each block line is suffixed with
    ``<!-- block:UUID -->`` so callers can target blocks for update/delete.
    """
    lines: list[str] = []
    prefix = "  " * indent
    numbered_idx = 0

    def _tag(block: dict) -> str:
        if include_ids:
            return f"  <!-- block:{block['id']} -->"
        return ""

    for block in blocks:
        btype = block.get("type", "")
        payload = block.get(btype, {})
        children = block.get("_children", [])

        tag = _tag(block)

        if btype == "paragraph":
            text = rich_text_to_md(payload.get("rich_text", []))
            lines.append(f"{prefix}{text}{tag}")
            lines.append("")

        elif btype in ("heading_1", "heading_2", "heading_3", "heading_4"):
            level = int(btype[-1])
            text = rich_text_to_md(payload.get("rich_text", []))
            lines.append(f"{prefix}{'#' * level} {text}{tag}")
            lines.append("")

        elif btype == "bulleted_list_item":
            text = rich_text_to_md(payload.get("rich_text", []))
            lines.append(f"{prefix}- {text}{tag}")
            if children:
                lines.append(blocks_to_markdown(children, indent + 1, include_ids))
            numbered_idx = 0

        elif btype == "numbered_list_item":
            numbered_idx += 1
            text = rich_text_to_md(payload.get("rich_text", []))
            lines.append(f"{prefix}{numbered_idx}. {text}{tag}")
            if children:
                lines.append(blocks_to_markdown(children, indent + 1, include_ids))

        elif btype == "to_do":
            text = rich_text_to_md(payload.get("rich_text", []))
            mark = "x" if payload.get("checked") else " "
            lines.append(f"{prefix}- [{mark}] {text}{tag}")
            if children:
                lines.append(blocks_to_markdown(children, indent + 1, include_ids))

        elif btype == "quote":
            text = rich_text_to_md(payload.get("rich_text", []))
            qlines = text.split("\n")
            lines.append(f"{prefix}> {qlines[0]}{tag}")
            for qline in qlines[1:]:
                lines.append(f"{prefix}> {qline}")
            if children:
                child_md = blocks_to_markdown(children, indent, include_ids)
                for cline in child_md.split("\n"):
                    if cline.strip():
                        lines.append(f"{prefix}> {cline}")
            lines.append("")

        elif btype == "callout":
            icon_obj = payload.get("icon", {})
            icon = ""
            if icon_obj.get("type") == "emoji":
                icon = icon_obj.get("emoji", "") + " "
            text = rich_text_to_md(payload.get("rich_text", []))
            lines.append(f"{prefix}> {icon}{text}{tag}")
            if children:
                child_md = blocks_to_markdown(children, indent, include_ids)
                for cline in child_md.split("\n"):
                    if cline.strip():
                        lines.append(f"{prefix}> {cline}")
            lines.append("")

        elif btype == "code":
            text = rich_text_to_md(payload.get("rich_text", []))
            lang = payload.get("language", "")
            lines.append(f"{prefix}```{lang}{tag}")
            lines.append(text)
            lines.append(f"{prefix}```")
            lines.append("")

        elif btype == "divider":
            lines.append(f"{prefix}---{tag}")
            lines.append("")

        elif btype == "toggle":
            text = rich_text_to_md(payload.get("rich_text", []))
            lines.append(f"{prefix}> **{text}**{tag}")
            if children:
                child_md = blocks_to_markdown(children, indent, include_ids)
                for cline in child_md.split("\n"):
                    if cline.strip():
                        lines.append(f"{prefix}> {cline}")
            lines.append("")

        elif btype == "bookmark":
            url = payload.get("url", "")
            caption = rich_text_to_md(payload.get("caption", []))
            label = caption or url
            lines.append(f"{prefix}[{label}]({url}){tag}")
            lines.append("")

        elif btype == "image":
            url = _extract_file_url(payload)
            caption = rich_text_to_md(payload.get("caption", []))
            lines.append(f"{prefix}![{caption}]({url}){tag}")
            lines.append("")

        elif btype in ("file", "pdf", "video", "audio"):
            url = _extract_file_url(payload)
            name = payload.get("name", btype)
            caption = rich_text_to_md(payload.get("caption", []))
            label = caption or name
            lines.append(f"{prefix}[{label}]({url}) ({btype}){tag}")
            lines.append("")

        elif btype == "embed":
            url = payload.get("url", "")
            lines.append(f"{prefix}[embed: {url}]{tag}")
            lines.append("")

        elif btype == "equation":
            expr = payload.get("expression", "")
            lines.append(f"{prefix}$${expr}$${tag}")
            lines.append("")

        elif btype == "table":
            if children:
                lines.append(_render_table(children, prefix))
            lines.append("")

        elif btype == "child_page":
            title = payload.get("title", "(untitled)")
            lines.append(f"{prefix}[Page: {title}] (id: {block['id']})")
            lines.append("")

        elif btype == "child_database":
            title = payload.get("title", "(untitled)")
            lines.append(f"{prefix}[Database: {title}] (id: {block['id']})")
            lines.append("")

        elif btype == "link_to_page":
            target = payload.get("page_id", "") or payload.get("database_id", "")
            lines.append(f"{prefix}[Link to: {target}]{tag}")
            lines.append("")

        elif btype == "table_of_contents":
            lines.append(f"{prefix}[Table of Contents]{tag}")
            lines.append("")

        elif btype == "column_list":
            for i, col in enumerate(children):
                col_children = col.get("_children", [])
                if col_children:
                    lines.append(blocks_to_markdown(col_children, indent, include_ids))
                if i < len(children) - 1:
                    lines.append(f"{prefix}|")
            lines.append("")

        elif btype == "synced_block":
            if children:
                lines.append(blocks_to_markdown(children, indent, include_ids))

        elif btype == "tab":
            # Each direct child is a paragraph: its text is the tab label,
            # its children the tab's content.
            for tab in children:
                tab_payload = tab.get("paragraph", {})
                label = rich_text_to_md(tab_payload.get("rich_text", []))
                emoji = (tab_payload.get("icon") or {}).get("emoji", "")
                shown = f"{emoji} {label}" if emoji else label
                lines.append(f"{prefix}**[Tab: {shown}]**{_tag(tab)}")
                tab_children = tab.get("_children", [])
                if tab_children:
                    lines.append(
                        blocks_to_markdown(tab_children, indent + 1, include_ids)
                    )
            lines.append("")

        elif btype == "meeting_notes":
            title = rich_text_to_md(payload.get("title", [])) or "(untitled)"
            status = payload.get("status", "")
            lines.append(
                f"{prefix}[Meeting notes: {title}] ({status}) "
                f"(id: {block['id']} — read with notion_read_meeting_notes)"
            )
            lines.append("")

        else:
            text = rich_text_to_md(payload.get("rich_text", []))
            if text:
                lines.append(f"{prefix}{text} [{btype}]{tag}")
            else:
                lines.append(f"{prefix}[{btype} block]{tag}")
            lines.append("")

        # Reset numbered index for non-numbered items
        if btype != "numbered_list_item":
            numbered_idx = 0

    return "\n".join(lines).rstrip()


def _extract_file_url(payload: dict) -> str:
    src_type = payload.get("type", "")
    if src_type == "file":
        return payload.get("file", {}).get("url", "")
    elif src_type == "external":
        return payload.get("external", {}).get("url", "")
    return ""


def _render_table(rows: list[dict], prefix: str) -> str:
    lines: list[str] = []
    for i, row in enumerate(rows):
        cells = row.get("table_row", {}).get("cells", [])
        cell_texts = [rich_text_to_md(cell) for cell in cells]
        lines.append(f"{prefix}| " + " | ".join(cell_texts) + " |")
        if i == 0:
            lines.append(f"{prefix}| " + " | ".join("---" for _ in cells) + " |")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Markdown -> Blocks
# ---------------------------------------------------------------------------

def markdown_to_blocks(text: str) -> list[dict]:
    """Parse markdown text into Notion block objects.

    Supports: headings, paragraphs, bullet/numbered/to-do lists, quotes,
    fenced code blocks, dividers.  Inline formatting (**bold**, *italic*,
    ``code``, ~~strike~~, [links](url)) is preserved.
    """
    blocks: list[dict] = []
    lines = text.split("\n")
    i = 0

    while i < len(lines):
        line = lines[i]

        # Empty line — skip
        if not line.strip():
            i += 1
            continue

        # Fenced code block
        if line.strip().startswith("```"):
            lang = line.strip()[3:].strip() or "plain text"
            code_lines: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            if i < len(lines):
                i += 1  # skip closing ```
            blocks.append({
                "object": "block",
                "type": "code",
                "code": {
                    "rich_text": [_plain_rt("\n".join(code_lines))],
                    "language": lang,
                },
            })
            continue

        # Divider
        if line.strip() in ("---", "***", "___"):
            blocks.append({"object": "block", "type": "divider", "divider": {}})
            i += 1
            continue

        # Headings (heading_4 exists API-side since 2026-03; deeper
        # markdown levels clamp to heading_4 — Notion has no heading_5+)
        m = re.match(r"^(#{1,6})\s+(.+)$", line)
        if m:
            level = min(len(m.group(1)), 4)
            btype = f"heading_{level}"
            blocks.append({
                "object": "block",
                "type": btype,
                btype: {"rich_text": md_to_rich_text(m.group(2))},
            })
            i += 1
            continue

        # To-do
        m = re.match(r"^[-*]\s+\[([ xX])\]\s+(.+)$", line)
        if m:
            blocks.append({
                "object": "block",
                "type": "to_do",
                "to_do": {
                    "rich_text": md_to_rich_text(m.group(2)),
                    "checked": m.group(1).lower() == "x",
                },
            })
            i += 1
            continue

        # Bulleted list
        m = re.match(r"^[-*]\s+(.+)$", line)
        if m:
            blocks.append({
                "object": "block",
                "type": "bulleted_list_item",
                "bulleted_list_item": {"rich_text": md_to_rich_text(m.group(1))},
            })
            i += 1
            continue

        # Numbered list
        m = re.match(r"^\d+\.\s+(.+)$", line)
        if m:
            blocks.append({
                "object": "block",
                "type": "numbered_list_item",
                "numbered_list_item": {"rich_text": md_to_rich_text(m.group(1))},
            })
            i += 1
            continue

        # Quote (consecutive > lines)
        if line.startswith("> ") or line == ">":
            quote_lines: list[str] = []
            while i < len(lines) and (lines[i].startswith("> ") or lines[i] == ">"):
                quote_lines.append(lines[i][2:] if lines[i].startswith("> ") else "")
                i += 1
            blocks.append({
                "object": "block",
                "type": "quote",
                "quote": {"rich_text": md_to_rich_text("\n".join(quote_lines))},
            })
            continue

        # Paragraph (default)
        blocks.append({
            "object": "block",
            "type": "paragraph",
            "paragraph": {"rich_text": md_to_rich_text(line)},
        })
        i += 1

    return blocks


# ---------------------------------------------------------------------------
# Property value formatting (for display)
# ---------------------------------------------------------------------------

def format_property_value(prop: dict) -> str:
    """Format a Notion page property value as a readable string."""
    ptype = prop.get("type", "")

    if ptype == "title":
        return "".join(t.get("plain_text", "") for t in prop.get("title", []))
    if ptype == "rich_text":
        return "".join(t.get("plain_text", "") for t in prop.get("rich_text", []))
    if ptype == "number":
        val = prop.get("number")
        return str(val) if val is not None else ""
    if ptype == "select":
        sel = prop.get("select")
        return sel.get("name", "") if sel else ""
    if ptype == "multi_select":
        return ", ".join(o.get("name", "") for o in prop.get("multi_select", []))
    if ptype == "date":
        d = prop.get("date")
        if not d:
            return ""
        start = d.get("start", "")
        end = d.get("end")
        return f"{start} -> {end}" if end else start
    if ptype == "people":
        return ", ".join(
            p.get("name", p.get("id", "?")) for p in prop.get("people", [])
        )
    if ptype == "checkbox":
        return "Yes" if prop.get("checkbox") else "No"
    if ptype == "url":
        return prop.get("url", "") or ""
    if ptype == "email":
        return prop.get("email", "") or ""
    if ptype == "phone_number":
        return prop.get("phone_number", "") or ""
    if ptype == "files":
        return ", ".join(f.get("name", "") for f in prop.get("files", []))
    if ptype == "formula":
        f = prop.get("formula", {})
        return str(f.get(f.get("type", ""), ""))
    if ptype == "relation":
        return ", ".join(r.get("id", "") for r in prop.get("relation", []))
    if ptype == "rollup":
        r = prop.get("rollup", {})
        rtype = r.get("type", "")
        if rtype == "array":
            return ", ".join(format_property_value(item) for item in r.get("array", []))
        return str(r.get(rtype, ""))
    if ptype == "created_time":
        return prop.get("created_time", "")
    if ptype == "last_edited_time":
        return prop.get("last_edited_time", "")
    if ptype == "created_by":
        return prop.get("created_by", {}).get("name", "")
    if ptype == "last_edited_by":
        return prop.get("last_edited_by", {}).get("name", "")
    if ptype == "status":
        s = prop.get("status")
        return s.get("name", "") if s else ""
    if ptype == "unique_id":
        u = prop.get("unique_id", {})
        pfx = u.get("prefix", "")
        num = u.get("number", "")
        return f"{pfx}-{num}" if pfx else str(num)

    return str(prop.get(ptype, ""))


def format_page_properties(properties: dict) -> str:
    """Format all properties of a page as readable lines."""
    lines: list[str] = []
    for name, prop in properties.items():
        val = format_property_value(prop)
        if val:
            lines.append(f"  {name}: {val}")
    return "\n".join(lines)
