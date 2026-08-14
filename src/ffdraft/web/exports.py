"""Read and render the boards `/board` writes to `data/exports/`.

A board is a 600-line markdown document that is mostly tables, which makes it
close to unreadable as raw text — and until now reading one meant leaving the
app that produced it. This renders them in place.

Rendered HTML is served as a complete standalone document and shown in a
sandboxed iframe. Board text is written by an LLM, so it is not trusted input:
the sandbox means an embedded `<script>` cannot run no matter what the markdown
contained, without needing a sanitiser to be perfect.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import markdown

from ..config import project_root

EXPORTS_DIR = "data/exports"

# `/board` names its output board_<ISO-ish timestamp>.md. Anything else in the
# directory is not a board and is not offered.
_BOARD_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+\.md$")


@dataclass(frozen=True)
class Export:
    name: str
    size: int
    modified: str
    title: str

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def exports_dir() -> Path:
    return project_root() / EXPORTS_DIR


def _first_heading(path: Path) -> str:
    """The document's H1, which names the league and slot it was built for."""
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for _ in range(20):
                line = handle.readline()
                if not line:
                    break
                if line.startswith("# "):
                    return line[2:].strip()
    except OSError:
        pass
    return path.stem


def list_exports() -> list[Export]:
    """Every board, newest first."""
    directory = exports_dir()
    if not directory.is_dir():
        return []
    found = []
    for path in directory.glob("*.md"):
        if not _BOARD_NAME_RE.match(path.name):
            continue
        stat = path.stat()
        found.append(
            Export(
                name=path.name,
                size=stat.st_size,
                modified=datetime.fromtimestamp(stat.st_mtime, UTC).isoformat(timespec="seconds"),
                title=_first_heading(path),
            )
        )
    # Name breaks ties on mtime. `modified` is second-resolution and two boards
    # can land in the same second, which would otherwise leave the order down to
    # whatever glob returned. Export names embed their own timestamp, so they
    # sort lexically in the same direction and settle it deterministically.
    return sorted(found, key=lambda e: (e.modified, e.name), reverse=True)


def resolve(name: str) -> Path:
    """Map a requested name onto a real export, or raise.

    Validated against the actual listing rather than by inspecting the string:
    a name is only served if it is one this directory already offers, so
    traversal, absolute paths and symlinks out of the directory cannot resolve
    to anything regardless of how they are spelled.
    """
    for export in list_exports():
        if export.name == name:
            return exports_dir() / export.name
    raise FileNotFoundError(f"no export named {name!r}")


def read(name: str) -> str:
    return resolve(name).read_text(encoding="utf-8", errors="replace")


def headings(name: str) -> list[dict]:
    """A table of contents, for navigating a document this long.

    Built from the markdown source rather than the rendered HTML so the slugs
    match what `toc` generates, and fenced code blocks are skipped so a `#`
    comment inside one is not mistaken for a heading.
    """
    out: list[dict] = []
    in_fence = False
    for line in read(name).splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = re.match(r"^(#{1,4})\s+(.*)$", line)
        if match:
            text = match.group(2).strip()
            out.append({"level": len(match.group(1)), "text": text, "id": _slug(text)})
    return out


def _slug(text: str) -> str:
    """Match python-markdown's `toc` slugify so anchors line up."""
    text = re.sub(r"[^\w\s-]", "", text.lower()).strip()
    return re.sub(r"[-\s]+", "-", text)


def render(name: str) -> str:
    """A complete standalone HTML document for the given export."""
    body = markdown.markdown(
        read(name),
        extensions=["tables", "fenced_code", "toc", "sane_lists", "attr_list"],
        output_format="html",
    )
    return _DOCUMENT.replace("{{title}}", _escape(name)).replace("{{body}}", body)


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


# Styles live here rather than in the app's stylesheet because the document is
# rendered inside a sandboxed iframe, which cannot reach the parent page's CSS.
# Both themes are defined so the board follows the OS setting like the rest of
# the UI.
_DOCUMENT = """<!doctype html>
<html><head><meta charset="utf-8"><title>{{title}}</title><style>
:root {
  --bg:#fff; --text:#1a1d22; --dim:#6b7480; --line:#dfe3e9;
  --panel:#f7f8fa; --accent:#2563eb; --good:#0a7f4f; --bad:#c0392b;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg:#0f1216; --text:#e6e9ee; --dim:#8b95a3; --line:#262c35;
    --panel:#161a20; --accent:#4b9fff; --good:#3fbf7f; --bad:#ff6b6b;
  }
}
* { box-sizing: border-box; }
body {
  margin:0; padding:28px 34px 80px;
  font:15px/1.65 ui-sans-serif,-apple-system,"Segoe UI",system-ui,sans-serif;
  background:var(--bg); color:var(--text);
}
h1,h2,h3,h4 { line-height:1.25; margin:1.8em 0 .6em; scroll-margin-top:16px; }
h1 { font-size:24px; margin-top:0; }
h2 { font-size:19px; border-bottom:1px solid var(--line); padding-bottom:6px; }
h3 { font-size:16px; }
h4 { font-size:14px; color:var(--dim); }
p,li { margin:.5em 0; }
a { color:var(--accent); }
code {
  background:var(--panel); border:1px solid var(--line); border-radius:3px;
  padding:1px 5px; font:13px/1.4 ui-monospace,"SF Mono",Menlo,monospace;
}
pre {
  background:var(--panel); border:1px solid var(--line); border-radius:6px;
  padding:12px; overflow-x:auto;
}
pre code { background:none; border:none; padding:0; }
/* Tables are the whole point — a board is mostly tables. */
table {
  border-collapse:collapse; width:100%; margin:1em 0; font-size:13.5px;
  font-variant-numeric:tabular-nums; display:block; overflow-x:auto;
}
th,td { border:1px solid var(--line); padding:6px 10px; text-align:left; vertical-align:top; }
th { background:var(--panel); font-size:12px; text-transform:uppercase;
     letter-spacing:.4px; color:var(--dim); white-space:nowrap; }
tr:nth-child(even) td { background:color-mix(in srgb, var(--panel) 50%, transparent); }
blockquote {
  margin:1em 0; padding:.4em 0 .4em 14px;
  border-left:3px solid var(--line); color:var(--dim);
}
hr { border:none; border-top:1px solid var(--line); margin:2em 0; }
strong { font-weight:650; }
</style></head><body>
{{body}}
</body></html>
"""
