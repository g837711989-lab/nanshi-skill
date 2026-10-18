#!/usr/bin/env python3
import json, time, urllib.request, urllib.error, os, sys
from collections import deque

OUTDIR = "/workspace/notion-extract"

def api(host, path, body, retries=4):
    url = f"https://{host}/api/v3/{path}"
    data = json.dumps(body).encode()
    last = None
    for i in range(retries):
        req = urllib.request.Request(url, data=data, headers={
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "Accept": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            last = e
            b = e.read()[:200]
            if e.code in (429, 500, 502, 503, 504):
                time.sleep(0.6 * (i + 1))
                continue
            raise RuntimeError(f"HTTP {e.code} {path} {b}")
        except Exception as e:
            last = e
            time.sleep(0.5 * (i + 1))
    raise RuntimeError(f"failed {path}: {last}")

def load_chunk(host, block_id):
    body = {
        "page": {"id": block_id},
        "limit": 100,
        "cursor": {"stack": []},
        "chunkNumber": 0,
        "verticalColumns": False,
    }
    return api(host, "loadPageChunk", body)

def rich_text(prop):
    if not prop:
        return ""
    parts = []
    for seg in prop:
        if isinstance(seg, str):
            parts.append(seg)
            continue
        if not isinstance(seg, list) or not seg:
            continue
        text = seg[0] if isinstance(seg[0], str) else ""
        meta = seg[1] if len(seg) > 1 else None
        href = None
        if isinstance(meta, list):
            for m in meta:
                if isinstance(m, list) and m and m[0] == "a" and len(m) > 1:
                    href = m[1]
        if href and text and text != href:
            parts.append(f"{text} ({href})")
        elif href and not text:
            parts.append(str(href))
        else:
            parts.append(text)
    return "".join(parts).replace("\u00a0", " ")

class Crawler:
    def __init__(self, host):
        self.host = host
        self.blocks = {}
        self.fetched = set()
        self.reqs = 0

    def ingest(self, data, requested):
        self.reqs += 1
        blocks = data.get("recordMap", {}).get("block", {})
        for bid, rec in blocks.items():
            val = (rec or {}).get("value", {}).get("value")
            if isinstance(val, dict) and val.get("id"):
                # keep richest
                prev = self.blocks.get(bid)
                if prev is None or len(val.get("content") or []) >= len(prev.get("content") or []):
                    self.blocks[bid] = val
        self.fetched.add(requested)

    def ensure(self, block_id):
        if block_id in self.fetched:
            return
        data = load_chunk(self.host, block_id)
        self.ingest(data, block_id)

    def walk(self, block_id, depth, lines, seen, pages_found):
        if block_id in seen or depth > 30:
            return
        seen.add(block_id)
        if block_id not in self.blocks or (self.blocks[block_id].get("content") and block_id not in self.fetched):
            try:
                self.ensure(block_id)
            except Exception as e:
                lines.append("  " * depth + f"[FAILED TO LOAD {block_id}: {e}]")
                return
        v = self.blocks.get(block_id)
        if not v or v.get("alive") is False:
            return
        typ = v.get("type") or "?"
        title = rich_text((v.get("properties") or {}).get("title"))
        # record child pages
        if typ == "page" and depth > 0:
            pages_found.append((title or block_id, block_id))
        kids = v.get("content") or []
        # if children missing, we already fetched this id; if still missing they may be in the response
        missing = [k for k in kids if k not in self.blocks]
        if missing and block_id not in self.fetched:
            try:
                self.ensure(block_id)
            except Exception as e:
                lines.append("  " * depth + f"[FAILED children {block_id}: {e}]")
        # re-read
        v = self.blocks.get(block_id) or v
        kids = v.get("content") or []
        missing = [k for k in kids if k not in self.blocks]
        # fetch missing children individually (their own chunk brings their kids)
        for k in missing:
            try:
                self.ensure(k)
            except Exception as e:
                lines.append("  " * depth + f"[FAILED child {k}: {e}]")

        indent = "  " * depth
        skip_types = {"column_list", "column", "table", "table_row"}
        if typ == "page" and depth == 0:
            lines.append(f"# {title}")
        elif typ == "page":
            lines.append(f"{indent}## PAGE: {title}")
        elif typ in ("header", "sub_header", "sub_sub_header"):
            level = {"header": "##", "sub_header": "###", "sub_sub_header": "####"}[typ]
            lines.append(f"{indent}{level} {title}")
        elif typ == "toggle":
            lines.append(f"{indent}▼ {title}")
        elif typ == "bulleted_list":
            lines.append(f"{indent}- {title}")
        elif typ == "numbered_list":
            lines.append(f"{indent}1. {title}")
        elif typ == "to_do":
            checked = (v.get("properties") or {}).get("checked")
            mark = "x" if checked else " "
            lines.append(f"{indent}- [{mark}] {title}")
        elif typ == "quote":
            lines.append(f"{indent}> {title}")
        elif typ == "callout":
            lines.append(f"{indent}> {title}")
        elif typ == "code":
            lines.append(f"{indent}```\n{title}\n{indent}```")
        elif typ == "divider":
            lines.append(f"{indent}---")
        elif typ in ("image", "file", "pdf", "video", "audio", "bookmark", "embed", "link_to_page"):
            cap = rich_text((v.get("properties") or {}).get("caption"))
            src = ""
            fmt = v.get("format") or {}
            src = fmt.get("display_source") or (v.get("properties") or {}).get("source") or ""
            if isinstance(src, list):
                src = rich_text(src)
            lines.append(f"{indent}[{typ}] {title} {cap} {src}".rstrip())
        elif typ == "equation":
            lines.append(f"{indent}$$ {title}")
        elif typ in ("collection_view", "collection_view_page"):
            lines.append(f"{indent}[DATABASE] {title} id={block_id}")
        elif typ == "alias":
            lines.append(f"{indent}[alias] {title}")
        elif typ == "transclusion_container":
            if title:
                lines.append(f"{indent}{title}")
        elif typ == "transclusion_reference":
            pointer = ((v.get("format") or {}).get("transclusion_reference_pointer") or {}).get("id")
            lines.append(f"{indent}[synced block → {pointer}]")
            if pointer:
                self.walk(pointer, depth + 1, lines, seen, pages_found)
        elif typ == "text":
            if title.strip():
                lines.append(f"{indent}{title}")
        elif typ in skip_types:
            pass
        else:
            if title.strip():
                lines.append(f"{indent}{title}")
            else:
                # still descend
                pass

        child_depth = depth + 1 if typ not in ("column_list", "column", "table") else depth
        for k in kids:
            self.walk(k, child_depth, lines, seen, pages_found)

def crawl_page(host, page_id, name):
    c = Crawler(host)
    lines = []
    pages = []
    t0 = time.time()
    c.walk(page_id, 0, lines, set(), pages)
    text = "\n".join(lines)
    # collapse excessive blank lines
    out_lines = []
    blank = 0
    for ln in text.splitlines():
        if ln.strip() == "":
            blank += 1
            if blank <= 1:
                out_lines.append("")
        else:
            blank = 0
            out_lines.append(ln.rstrip())
    text = "\n".join(out_lines).strip() + "\n"
    path = os.path.join(OUTDIR, name + ".md")
    with open(path, "w") as f:
        f.write(text)
    print(f"WROTE {path} chars={len(text)} reqs={c.reqs} blocks={len(c.blocks)} pages={len(pages)} sec={time.time()-t0:.1f}")
    # types summary
    from collections import Counter
    types = Counter(b.get("type") for b in c.blocks.values())
    print(" types", dict(types))
    if pages:
        print(" child pages:")
        for t, i in pages[:80]:
            print("  -", t, i)
        if len(pages) > 80:
            print("  ...", len(pages)-80, "more")
    return path, len(text), pages

if __name__ == "__main__":
    host = sys.argv[1]
    page_id = sys.argv[2]
    name = sys.argv[3]
    # normalize id
    pid = page_id.replace("-", "")
    if len(pid) == 32:
        page_id = f"{pid[0:8]}-{pid[8:12]}-{pid[12:16]}-{pid[16:20]}-{pid[20:32]}"
    crawl_page(host, page_id, name)
