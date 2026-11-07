"""The prompt guides of prompt-guides.toml, whose header describes their scope and layout: the family a model
belongs to, and the download of the vendor references each family lists."""

import fnmatch
import hashlib
import io
import json
import re
import tomllib
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin

import httpx

from . import matching_names

PROMPT_GUIDES = tomllib.loads((Path(__file__).parent / "prompt-guides.toml").read_text(encoding="utf-8"))
TIMEOUT = 60


def family(category: str, model: str) -> str | None:
    """The prompt guide family whose globs match the model ID, on any endpoint."""
    return next(
        (
            name
            for name, entry in PROMPT_GUIDES.items()
            if entry["category"] == category
            and any(fnmatch.fnmatchcase(alias, glob) for alias in matching_names(model) for glob in entry["models"])
        ),
        None,
    )


async def get(http: httpx.AsyncClient, url: str) -> bytes:
    response = await http.get(url)
    response.raise_for_status()
    return response.content


async def skill_md(http: httpx.AsyncClient, index_url: str, name: str) -> bytes:
    """The SKILL.md of the named archive in an agent-skills index, checked against the index's sha256 digest."""
    index = json.loads(await get(http, index_url))
    entry = next((skill for skill in index["skills"] if skill["name"] == name), None)
    if entry is None:
        raise ValueError(f"no skill named {name} in {index_url}")
    archive = await get(http, entry["url"])
    algorithm, digest = entry["digest"].split(":", 1)
    if hashlib.new(algorithm, archive).hexdigest() != digest:
        raise ValueError(f"{entry['url']} does not match the index digest")
    with zipfile.ZipFile(io.BytesIO(archive)) as zf:
        member = min(
            (info.filename for info in zf.infolist() if Path(info.filename).name == "SKILL.md"),
            key=len,
        )
        return zf.read(member)


class Markdown(HTMLParser):
    """Converts the element that matches a tag#id or tag.class selector to Markdown."""

    BLOCKS = frozenset({"p", "div", "section", "article", "header", "footer", "figure", "figcaption", "dl", "dt", "dd"})
    SKIP = frozenset(
        {"script", "style", "noscript", "template", "svg", "video", "audio", "button", "nav", "aside", "form", "devsite-feedback"}
    )
    VOID = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"})

    def __init__(self, url: str, select: str):
        super().__init__(convert_charrefs=True)
        self.url = url
        self.tag, sep, value = re.split(r"([#.])", select, maxsplit=1)
        self.attr = "id" if sep == "#" else "class"
        self.value = value
        self.depth = 0  # open elements inside the selected one, counting the selected element
        self.skip = 0  # open elements inside a skipped element
        self.out: list[str] = []
        self.lists: list[list] = []  # [ordered, next number] per open list
        self.pre = 0
        self.item = False  # a list marker was written and no text has followed it yet
        self.href: str | None = None
        self.link: list[str] = []
        self.table: list[list[str]] | None = None
        self.cell: list[str] | None = None

    def matches(self, tag: str, attrs: dict) -> bool:
        if tag != self.tag:
            return False
        if self.attr == "id":
            return attrs.get("id") == self.value
        return self.value in (attrs.get("class") or "").split()

    def write(self, text: str) -> None:
        if text.strip():
            self.item = False
        if self.cell is not None:
            self.cell.append(text)
        elif self.href is not None:
            self.link.append(text)
        else:
            self.out.append(text)

    def block(self, prefix: str = "") -> None:
        if self.item and not prefix:
            return  # a paragraph opening a list item stays on the marker's line
        self.write("\n\n" + prefix)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if self.depth == 0:
            if self.matches(tag, attrs):
                self.depth = 1
            return
        if tag not in self.VOID:
            self.depth += 1
        if self.skip:
            if tag not in self.VOID:
                self.skip += 1
            return
        if tag in self.SKIP or attrs.get("hidden") is not None or attrs.get("aria-hidden") == "true":
            if tag not in self.VOID:
                self.skip = 1
            return
        if re.fullmatch(r"h[1-6]", tag):
            self.block("#" * int(tag[1]) + " ")
        elif tag in self.BLOCKS:
            self.block()
        elif tag == "br":
            self.write("\n")
        elif tag == "hr":
            self.block("---")
        elif tag in ("ul", "ol"):
            self.lists.append([tag == "ol", 1])
            if len(self.lists) == 1:
                self.block()
        elif tag == "li":
            indent = "  " * (len(self.lists) - 1)
            if self.lists and self.lists[-1][0]:
                marker = f"{self.lists[-1][1]}. "
                self.lists[-1][1] += 1
            else:
                marker = "- "
            self.write("\n" + indent + marker)
            self.item = True
        elif tag == "pre":
            self.pre += 1
            self.block("```\n")
        elif tag == "code" and not self.pre:
            self.write("`")
        elif tag in ("strong", "b"):
            self.write("**")
        elif tag in ("em", "i"):
            self.write("*")
        elif tag == "blockquote":
            self.block("> ")
        elif tag == "a" and attrs.get("href") and self.cell is None:
            self.href = urljoin(self.url, attrs["href"])
            self.link = []
        elif tag == "img" and attrs.get("alt"):
            self.write(f"[image: {attrs['alt']}]")
        elif tag == "table":
            self.table = []
        elif tag == "tr" and self.table is not None:
            self.table.append([])
        elif tag in ("td", "th") and self.table is not None:
            self.cell = []

    def handle_endtag(self, tag):
        if self.depth == 0 or tag in self.VOID:
            return
        self.depth -= 1
        if self.depth == 0:
            return
        if self.skip:
            self.skip -= 1
            return
        if re.fullmatch(r"h[1-6]", tag) or tag in self.BLOCKS or tag == "blockquote":
            self.block()
        elif tag in ("ul", "ol"):
            if self.lists:
                self.lists.pop()
            if not self.lists:
                self.block()
        elif tag == "pre":
            self.pre -= 1
            self.write("\n```")
            self.block()
        elif tag == "code" and not self.pre:
            self.write("`")
        elif tag in ("strong", "b"):
            self.write("**")
        elif tag in ("em", "i"):
            self.write("*")
        elif tag == "a" and self.href is not None:
            href, text = self.href, "".join(self.link).strip()
            self.href = None
            if text and not href.startswith("javascript:"):
                self.write(f"[{text}]({href})" if not href.startswith(self.url + "#") else text)
            else:
                self.write(text)
        elif tag in ("td", "th") and self.cell is not None:
            text = re.sub(r"\s+", " ", "".join(self.cell)).strip().replace("|", "\\|")
            self.cell = None
            if self.table:
                self.table[-1].append(text)
        elif tag == "table" and self.table is not None:
            rows, self.table = [row for row in self.table if row], None
            if rows:
                width = max(len(row) for row in rows)
                rows = [row + [""] * (width - len(row)) for row in rows]
                lines = ["| " + " | ".join(row) + " |" for row in rows]
                lines.insert(1, "|" + " --- |" * width)
                self.block("\n".join(lines))
                self.block()

    def handle_data(self, data):
        if self.depth == 0 or self.skip:
            return
        if self.pre:
            self.write(data)
        else:
            self.write(re.sub(r"\s+", " ", data))

    def markdown(self) -> str:
        text = "".join(self.out)
        lines = []
        fence = False
        for line in text.split("\n"):
            if line.startswith("```"):
                fence = not fence
            lines.append(line if fence else line.rstrip())
        text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines))
        return text.strip() + "\n"


def html_to_markdown(html: bytes, url: str, select: str) -> bytes:
    parser = Markdown(url, select)
    parser.feed(html.decode("utf-8", errors="replace"))
    parser.close()
    text = parser.markdown()
    if not text.strip():
        raise ValueError(f"no {select} element in the page")
    return f"Source: {url}\n\n{text}".encode()


async def download(http: httpx.AsyncClient, spec: dict, lang: str) -> bytes:
    url = spec["url"] if isinstance(spec["url"], str) else spec["url"][lang]
    if "skill" in spec:
        return await skill_md(http, url, spec["skill"])
    if "select" in spec:
        return html_to_markdown(await get(http, url), url, spec["select"])
    return await get(http, url)


async def fetch(files: list[dict], directory: Path, lang: str, update: bool) -> list[dict]:
    """Each file's path, kind and status: present, downloaded or missing, with the error when a download failed.

    A file is downloaded when it is missing, or every time with update; a failed download keeps the existing file.
    """
    results = []
    async with httpx.AsyncClient(
        follow_redirects=True, timeout=TIMEOUT, headers={"User-Agent": "multimodal-mcp"}
    ) as http:
        for spec in files:
            path = directory / spec["name"]
            result = {"path": str(path), "kind": spec["kind"]}
            if path.exists() and not update:
                result["status"] = "present"
            else:
                try:
                    data = await download(http, spec, lang)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    partial = path.with_name(path.name + ".part")
                    partial.write_bytes(data)
                    partial.replace(path)
                    result["status"] = "downloaded"
                except (httpx.HTTPError, OSError, ValueError, KeyError, zipfile.BadZipFile) as error:
                    result["status"] = "present" if path.exists() else "missing"
                    result["error"] = f"download failed: {type(error).__name__}: {error}"
            results.append(result)
    return results
