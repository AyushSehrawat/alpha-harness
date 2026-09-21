"""Cache the BRAIN documentation locally as searchable markdown.

Run this occasionally — the docs change (page `lastModified` dates move, and themes come and
go), and a stale copy is worse than none when the rules are what you are checking against.

    # easiest: borrow the session the harness already holds
    python scripts/fetch_brain_docs.py --from-harness

    # or supply the `t` cookie from platform.worldquantbrain.com yourself
    python scripts/fetch_brain_docs.py --token "<jwt>"
    BRAIN_T="<jwt>" python scripts/fetch_brain_docs.py

    python scripts/fetch_brain_docs.py --from-harness --check       # report drift, write nothing
    python scripts/fetch_brain_docs.py --from-harness --no-images   # markdown only

How to get the token by hand: open platform.worldquantbrain.com signed in, DevTools >
Application > Cookies, copy the value of `t`. It is a JWT and it expires — if every page
401s, fetch a new one. Nothing is written to disk except the docs themselves; the token is
never persisted, and `--from-harness` never prints it.

Why markdown, one file per page: it greps. The whole point is answering "what is the actual
rule for X" in one `grep -ri` instead of a network round trip, so plain text beats JSON or a
database here, and per-page files keep grep hits pointing at something readable.

Images sit behind the same cookie as the API, so they are downloaded rather than linked — a
remote URL in the cache is unreadable the moment the token expires.

Layout:

    .cache/docs/INDEX.md                      every page, grouped by category
    .cache/docs/_img/<file>                   every image any page references
    .cache/docs/<category>/<tutorial>__<page>.md

Source endpoints (the docs site is a SPA; these are what it calls):

    GET /tutorials?limit=50        the 11 tutorials and their page ids
    GET /tutorial-pages/{page_id}  one page's content blocks
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

API = "https://api.worldquantbrain.com"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, ".cache", "docs")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36")

# The API reports these two spellings for the same thing.
CATEGORY_ALIASES = {"Consultant Info": "Consultant Information"}

HEAD_KEYS = ("content", "heading_text", "heading", "text", "title", "value", "name")
IMG_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")

# Inline images arrive with no filename in the URL, so the extension comes off the bytes.
EXT_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"\xff\xd8\xff", ".jpg"),
    (b"GIF8", ".gif"),
    (b"BM", ".bmp"),
    (b"II*\x00", ".tif"),
    (b"MM\x00*", ".tif"),
)


def headers(token: str) -> dict[str, str]:
    return {
        "accept": "application/json;version=2.0",
        "accept-language": "en",
        "referer": "https://platform.worldquantbrain.com/",
        "origin": "https://platform.worldquantbrain.com",
        "user-agent": UA,
        "cookie": f"t={token}",
    }


def get(path: str, token: str, tries: int = 4):
    for attempt in range(tries):
        try:
            req = urllib.request.Request(API + path, headers=headers(token))
            with urllib.request.urlopen(req, timeout=90) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            if e.code in (429, 502, 503):
                time.sleep(10 * (attempt + 1))
                continue
            if e.code in (401, 403):
                sys.exit(f"auth failed ({e.code}) on {path} — the token has expired")
            print(f"   HTTP {e.code} {path}", file=sys.stderr)
            return None
        except Exception:
            time.sleep(3)
    return None


def get_bytes(url: str, token: str, tries: int = 3) -> bytes | None:
    hdr = dict(headers(token), accept="image/avif,image/webp,image/*,*/*;q=0.8")
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers=hdr)
            with urllib.request.urlopen(req, timeout=90) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code in (429, 502, 503):
                time.sleep(5 * (attempt + 1))
                continue
            print(f"   HTTP {e.code} {url}", file=sys.stderr)
            return None
        except Exception:
            time.sleep(2)
    return None


def img_tag_md(m: re.Match) -> str:
    tag = m.group(0)
    src = re.search(r'src="([^"]+)"', tag) or re.search(r"src='([^']+)'", tag)
    alt = re.search(r'alt="([^"]*)"', tag) or re.search(r"alt='([^']*)'", tag)
    return f"![{alt.group(1) if alt else ''}]({src.group(1)})" if src else ""


def to_md(raw: str) -> str:
    """Wagtail rich-text HTML to markdown, good enough to read and to grep."""
    if not raw:
        return ""
    s = raw
    # Pygments ships its whole stylesheet inline beside every code block; drop it first or
    # the CSS lands in the prose.
    s = re.sub(r"<(style|script)\b[^>]*>.*?</\1>", "", s, flags=re.S | re.I)
    s = re.sub(r"(?m)^\s*(pre|td\.linenos|span\.linenos|\.codehilite)[^\n{]*\{[^}]*\}\s*$", "", s)
    s = re.sub(r"<pre[^>]*>(.*?)</pre>",
               lambda m: "\n```\n" + re.sub(r"<[^>]+>", "", m.group(1)) + "\n```\n",
               s, flags=re.S)
    s = re.sub(r"<br\s*/?>", "\n", s)
    # Must precede the generic tag strip below, or inline figures vanish without a trace.
    s = re.sub(r"<img\b[^>]*>", img_tag_md, s, flags=re.I)
    s = re.sub(r'<a [^>]*href="([^"]+)"[^>]*>(.*?)</a>', r"[\2](\1)", s, flags=re.S)
    s = re.sub(r"<(b|strong)>(.*?)</\1>", r"**\2**", s, flags=re.S)
    s = re.sub(r"<(i|em)>(.*?)</\1>", r"*\2*", s, flags=re.S)
    s = re.sub(r"<code>(.*?)</code>", r"`\1`", s, flags=re.S)
    for n in range(1, 7):
        s = re.sub(rf"<h{n}[^>]*>(.*?)</h{n}>", "\n" + "#" * min(n + 1, 6) + r" \1\n",
                   s, flags=re.S)
    s = re.sub(r"<li[^>]*>(.*?)</li>", r"- \1\n", s, flags=re.S)
    s = re.sub(r"</?(ul|ol)[^>]*>", "\n", s)
    s = re.sub(r"<p[^>]*>", "\n", s)
    s = re.sub(r"</p>", "\n", s)
    s = re.sub(r"<th[^>]*>(.*?)</th>", r"| \1 ", s, flags=re.S)
    s = re.sub(r"<td[^>]*>(.*?)</td>", r"| \1 ", s, flags=re.S)
    s = re.sub(r"</tr>", "|\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t]+\n", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def dump(kind: str, value) -> str:
    """Unknown block shape: keep the payload rather than drop it. Bare placeholders silently
    swallowed 424 blocks — including the per-region submission-criteria table — for a whole
    cache generation."""
    return f"_[{kind}]_ " + json.dumps(value, ensure_ascii=False)[:8000]


def heading_md(value) -> str:
    if isinstance(value, str):
        text, size = (to_md(value) if "<" in value else value), 2
    elif isinstance(value, dict):
        text = next((str(value[k]) for k in HEAD_KEYS if value.get(k)), "")
        text = to_md(text) if "<" in text else text
        size = int(re.sub(r"\D", "", str(value.get("size") or value.get("level") or "2")) or 2)
    else:
        return dump("HEADING", value)
    return ("#" * max(2, min(size, 6)) + " " + text.strip()) if text.strip() \
        else dump("HEADING", value)


def cell_md(c) -> str:
    return (to_md(str(c)) if c is not None else "").replace("|", "\\|").replace("\n", " ").strip()


def table_md(value) -> str:
    rows = value if isinstance(value, list) else None
    caption, header_first = "", True
    if isinstance(value, dict):
        rows = value.get("data") or value.get("rows") or value.get("cells")
        caption = value.get("table_caption") or value.get("caption") or ""
        header_first = bool(value.get("first_row_is_table_header", True))
    if not isinstance(rows, list) or not rows:
        return dump("TABLE", value)

    grid = []
    for row in rows:
        if isinstance(row, dict):
            row = row.get("values") or row.get("cells") or list(row.values())
        grid.append([cell_md(c) for c in (row if isinstance(row, list) else [row])])
    width = max(len(r) for r in grid)
    grid = [r + [""] * (width - len(r)) for r in grid]

    head = grid[0] if header_first else [""] * width
    out = ["| " + " | ".join(head) + " |", "|" + "---|" * width]
    out += ["| " + " | ".join(r) + " |" for r in (grid[1:] if header_first else grid)]
    return (f"**{caption}**\n\n" if caption else "") + "\n".join(out)


SETTING_ORDER = ("instrumentType", "region", "universe", "delay", "decay", "neutralization",
                 "truncation", "pasteurization", "unitHandling", "nanHandling", "language",
                 "maxTrade", "visualization", "testPeriod")


def sim_md(value) -> str:
    """A worked example: the expression plus the settings it was simulated under. Both matter
    — the same expression is a different alpha at a different delay or universe."""
    if not isinstance(value, dict):
        return dump("SIMULATION_EXAMPLE", value)
    kind = (value.get("type") or "REGULAR").lower()
    code = value.get(kind) or value.get("regular") or value.get("code") or value.get("expression")
    if isinstance(code, dict):
        code = "\n\n".join(f"# {k}\n{v}" for k, v in code.items() if v)
    if not code:
        return dump("SIMULATION_EXAMPLE", value)

    settings = value.get("settings") or {}
    keys = [k for k in SETTING_ORDER if k in settings]
    keys += [k for k in settings if k not in SETTING_ORDER]
    shown = " · ".join(f"{k} {settings[k]}" for k in keys)
    lang = str(settings.get("language") or "").lower()
    out = f"```{'python' if lang == 'python' else ''}\n{str(code).replace(chr(13), '')}\n```"
    return f"{out}\n\n_{value.get('type') or 'REGULAR'}_ · {shown}" if shown else out


def block_md(b: dict) -> str:
    kind = (b.get("type") or "").upper()
    value = b.get("value")
    if kind == "TEXT":
        return to_md(value if isinstance(value, str) else "")
    if kind == "HEADING":
        return heading_md(value)
    if kind == "TABLE":
        return table_md(value)
    if kind == "SIMULATION_EXAMPLE":
        return sim_md(value)
    if kind == "EQUATION" and isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        if kind == "CODE" or "code" in value:
            return f"```{value.get('language') or ''}\n{value.get('code') or value.get('value') or ''}\n```"
        url = value.get("url") or value.get("src") or ""
        cap = value.get("caption") or value.get("title") or ""
        if kind == "IMAGE":
            return f"![{cap}]({url})" if url else f"_[IMAGE]_ {cap} uuid={value.get('uuid') or ''}".strip()
        if url:
            return f"_[{kind}]_ {cap} {url}".strip()
        return dump(kind, value)
    if isinstance(value, str):
        return f"_[{kind}]_ {to_md(value) if '<' in value else value}"
    return dump(kind, value) if value else f"_[{kind}]_"


def sniff_ext(blob: bytes) -> str:
    for magic, ext in EXT_MAGIC:
        if blob.startswith(magic):
            return ext
    if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        return ".webp"
    head = blob[:400].lstrip()
    if head.startswith(b"<svg") or (head.startswith(b"<?xml") and b"<svg" in blob[:800]):
        return ".svg"
    return ""


def store_image(blob: bytes, root: str, ext: str, img_dir: str) -> str:
    """Write the image under a name that never collides with a different one."""
    root = root or "img"
    ext = ext or sniff_ext(blob)
    name, n = root + ext, 1
    while True:
        path = os.path.join(img_dir, name)
        if not os.path.exists(path):
            with open(path, "wb") as f:
                f.write(blob)
            return name
        with open(path, "rb") as f:
            if f.read() == blob:
                return name
        n += 1
        name = f"{root}-{n}{ext}"


def localize_images(md: str, token: str, img_dir: str, seen: dict[str, str]) -> str:
    def repl(m: re.Match) -> str:
        alt, url = m.group(1), m.group(2).strip()
        if not url.startswith(("http://", "https://", "/")):
            return m.group(0)
        full = url if url.startswith("http") else API + url
        if full not in seen:
            root, ext = os.path.splitext(slug(os.path.basename(full.split("?")[0]), 120))
            root = root or slug(alt, 60) or f"img{len(seen)}"
            # A named file already on disk is the same image; an unnamed one has to be
            # fetched before its extension can be read off the bytes.
            if ext and os.path.exists(os.path.join(img_dir, root + ext)):
                seen[full] = root + ext
            else:
                blob = get_bytes(full, token)
                if blob is None:
                    return m.group(0)
                seen[full] = store_image(blob, root, ext, img_dir)
        return f"![{alt}](../_img/{seen[full]})"
    return IMG_RE.sub(repl, md)


def slug(s: str, n: int = 90) -> str:
    return re.sub(r"-{2,}", "-", re.sub(r"[^a-zA-Z0-9._-]+", "-", s or "")).strip("-")[:n]


def token_from_harness() -> str:
    """Read the `t` cookie out of the harness's own sealed jar, so a refresh does not mean
    copying a JWT out of DevTools. Returned, never printed or written."""
    import json as _json
    import sqlite3

    sys.path.insert(0, os.path.join(ROOT, "backend", "src"))
    from alpha_harness.config import get_settings  # noqa: PLC0415
    from alpha_harness.sealing import Sealer  # noqa: PLC0415

    data = str(get_settings().data_dir)
    try:
        sealer = Sealer(open(os.path.join(data, "key"), "rb").read())
    except OSError:
        sys.exit(f"no key at {data}/key — run the harness once, or pass --token")

    db = os.path.join(data, "harness.db")
    con = sqlite3.connect(f"file:{db.replace(os.sep, '/')}?mode=ro", uri=True)
    try:
        row = con.execute(
            "SELECT cookies_sealed FROM brain_session ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
    finally:
        con.close()
    if row is None:
        sys.exit("no stored BRAIN session — sign in on the harness first, or pass --token")

    jar = _json.loads(sealer.open(row[0], context="brain-cookies"))
    token = next((c.get("value") for c in jar if c.get("name") == "t"), "")
    if not token:
        sys.exit("the stored jar has no `t` cookie — sign in again, or pass --token")
    return token


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--token", default=os.environ.get("BRAIN_T", ""),
                    help="the `t` cookie from platform.worldquantbrain.com (or set BRAIN_T)")
    ap.add_argument("--from-harness", action="store_true",
                    help="borrow the session already stored in the harness database")
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--check", action="store_true",
                    help="compare lastModified against the local copy and write nothing")
    ap.add_argument("--no-images", action="store_true",
                    help="keep remote image URLs instead of downloading them to _img/")
    ap.add_argument("--pause", type=float, default=0.4, help="seconds between page fetches")
    args = ap.parse_args()
    if args.from_harness and not args.token:
        args.token = token_from_harness()
    if not args.token:
        sys.exit("no token: pass --token, set BRAIN_T, or use --from-harness")

    index = get("/tutorials?limit=50", args.token)
    if not index:
        sys.exit("could not fetch the tutorial index")
    tutorials = sorted(index["results"], key=lambda t: t.get("sequence") or 0)
    total = sum(len(t.get("pages") or []) for t in tutorials)
    print(f"{len(tutorials)} tutorials, {total} pages")

    # --check: report drift only, no writes and no page fetches
    if args.check:
        stale = new = 0
        for t in tutorials:
            cat = CATEGORY_ALIASES.get(t.get("category"), t.get("category") or "Other")
            for p in t.get("pages") or []:
                path = os.path.join(args.out, slug(cat), f"{slug(t['id'])}__{slug(p['id'])}.md")
                if not os.path.exists(path):
                    print(f"  NEW    {t['id']}/{p['id']}")
                    new += 1
                    continue
                with open(path, encoding="utf-8") as f:
                    head = f.read(1200)
                m = re.search(r"lastModified: (.+)", head)
                if m and m.group(1).strip() != str(p.get("lastModified")):
                    print(f"  STALE  {t['id']}/{p['id']}")
                    stale += 1
        print(f"\n{new} new, {stale} stale, {total - new - stale} current")
        return

    img_dir = os.path.join(args.out, "_img")
    os.makedirs(img_dir, exist_ok=True)
    seen_imgs: dict[str, str] = {}

    by_cat: dict[str, list[str]] = {}
    written = failed = 0
    for t in tutorials:
        cat = CATEGORY_ALIASES.get(t.get("category"), t.get("category") or "Other")
        cat_dir = os.path.join(args.out, slug(cat))
        os.makedirs(cat_dir, exist_ok=True)
        lines = by_cat.setdefault(cat, [])
        lines.append(f"\n### {t.get('title') or t['id']}  ·  `{t['id']}`\n")
        for p in t.get("pages") or []:
            pid = p["id"]
            data = get(f"/tutorial-pages/{pid}", args.token)
            if not data:
                failed += 1
                lines.append(f"- FAILED `{pid}`")
                continue
            body = "\n\n".join(
                x for x in (block_md(b) for b in (data.get("content") or [])) if x)
            if not args.no_images:
                body = localize_images(body, args.token, img_dir, seen_imgs)
            fname = f"{slug(t['id'])}__{slug(pid)}.md"
            with open(os.path.join(cat_dir, fname), "w", encoding="utf-8") as f:
                f.write(
                    f"# {data.get('title') or pid}\n\n"
                    f"- category: {cat}\n"
                    f"- tutorial: `{t['id']}`\n"
                    f"- page id: `{pid}`\n"
                    f"- source: https://platform.worldquantbrain.com/learn/documentation/"
                    f"{t['id']}/{pid}\n"
                    f"- lastModified: {data.get('lastModified')}\n\n---\n\n{body}\n")
            written += 1
            lines.append(f"- [{data.get('title') or pid}]({slug(cat)}/{fname})")
            print(f"  {written:>3}/{total} {slug(cat)}/{fname}", flush=True)
            time.sleep(args.pause)

    idx = [
        "# BRAIN documentation — local cache",
        "",
        f"{written} pages, {len(seen_imgs)} images, fetched {time.strftime('%Y-%m-%d')}. "
        "Grep this folder instead of re-fetching:",
        "",
        "```",
        "grep -ril 'power pool' .cache/docs/",
        "grep -ri -A5 'sub-universe' .cache/docs/consultant-information/",
        "```",
        "",
        "Refresh with `python scripts/fetch_brain_docs.py --token \"<jwt>\"`, "
        "or `--check` to see what changed upstream without writing.",
    ]
    for cat in sorted(by_cat):
        idx.append(f"\n## {cat}")
        idx += by_cat[cat]
    with open(os.path.join(args.out, "INDEX.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(idx) + "\n")

    # A page that moved between tutorials leaves its old file behind, and a cache quietly
    # mixing two vintages is how a stale rendering survives a re-fetch.
    fresh = {os.path.normcase(os.path.join(args.out, slug(c), l.split("](")[1].rstrip(")").split("/")[-1]))
             for c, ls in by_cat.items() for l in ls if "](" in l}
    orphans = [os.path.join(r, f) for r, _, fs in os.walk(args.out) for f in fs
               if f.endswith(".md") and f != "INDEX.md"
               and os.path.normcase(os.path.join(r, f)) not in fresh]
    for o in sorted(orphans):
        print(f"  orphan (not in the upstream index): {os.path.relpath(o, args.out)}")

    print(f"\nDONE: {written} written, {failed} failed, {len(seen_imgs)} images -> {args.out}")


if __name__ == "__main__":
    main()
