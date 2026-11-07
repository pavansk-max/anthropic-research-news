"""Find new research posts from Anthropic, OpenAI and Google DeepMind and rebuild index.html.

Sources:
  Anthropic  sitemap -> /research/<slug> pages (team and topics read from each page)
  OpenAI     news RSS feed, keeping the Research, Publication and Safety & Alignment categories
  DeepMind   sitemap -> /blog/<slug>/ pages, keeping the Research, Science and
             Responsibility & Safety sections

Only pages not already in posts.json are fetched. posts.json and index.html
(from template.html) are rewritten when something new turns up. Uses only the
Python standard library.
"""
import gzip
import html
import json
import re
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from email.utils import parsedate_to_datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
UA = "Mozilla/5.0 (research-news updater)"

ANTHROPIC_TEAM_PAGES = {"alignment", "economics", "frontier-red-team", "interpretability", "societal-impacts", "science", "engineering"}
OPENAI_CATEGORIES = {"Research", "Publication", "Safety & Alignment"}
DEEPMIND_SECTIONS = {"Research", "Science", "Responsibility & Safety"}


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=30) as r:
        body = r.read()
    if body[:2] == b"\x1f\x8b":
        body = gzip.decompress(body)
    return body.decode("utf-8", errors="ignore")


def fetch_all(urls):
    """Fetch pages a few at a time; returns {url: html or Exception}."""
    def one(u):
        try:
            return u, get(u)
        except Exception as e:
            return u, e
    with ThreadPoolExecutor(max_workers=6) as pool:
        return dict(pool.map(one, urls))


def meta_tags(s):
    """{property-or-name: content} for every <meta>, quoted or unquoted attributes."""
    out = {}
    for tag in re.findall(r"<meta\b[^>]*>", s):
        attrs = dict((k.lower(), html.unescape(v1 or v2 or v3)) for k, v1, v2, v3 in
                     re.findall(r'([\w:-]+)=(?:"([^"]*)"|\'([^\']*)\'|([^\s>"\']+))', tag))
        key = attrs.get("property") or attrs.get("name")
        if key and "content" in attrs:
            out.setdefault(key, attrs["content"])
    return out


# ---------- Anthropic ----------

def anthropic_new(known):
    xml = get("https://www.anthropic.com/sitemap.xml")
    slugs = re.findall(r"<loc>https://www\.anthropic\.com/research/([^<]+)</loc>", xml)
    slugs = [s for s in slugs if "/" not in s and s not in ANTHROPIC_TEAM_PAGES]
    if not slugs:
        raise RuntimeError("no research pages in the Anthropic sitemap")
    new = [s for s in slugs if s not in known]
    print(f"Anthropic: {len(slugs)} research pages, {len(new)} new")
    pages = fetch_all(["https://www.anthropic.com/research/" + s for s in new])
    added = []
    for slug in new:
        s = pages["https://www.anthropic.com/research/" + slug]
        if isinstance(s, Exception):
            print(f"  skip {slug}: {s}")
            continue
        m = meta_tags(s)
        title = re.sub(r"\s*[|·]\s*(Anthropic|Claude Academy)\s*$", "", m.get("og:title", "")).strip()
        if not title:
            print(f"  skip {slug}: no title found")
            continue
        desc = m.get("og:description") or m.get("description", "")
        if desc.startswith("Anthropic is an AI safety and research company"):
            # some pages carry the site tagline in their meta tags; the JSON-LD block has the real summary
            ld = re.search(r'"description"\s*:\s*("(?:[^"\\]|\\.)*")', s)
            desc = json.loads(ld.group(1)) if ld and "Anthropic is an AI safety" not in ld.group(1) else ""
        d = re.search(r'"datePublished"\s*:\s*"(\d{4}-\d{2}-\d{2})', s) or re.search(r'dateTime="(\d{4}-\d{2}-\d{2})', s)
        team = re.search(r'<p class="text-caption-1">Team</p><p class="text-caption-1">(.*?)</p></li>', s)
        tp = re.search(r'<p class="text-caption-1">Topics</p><ul[^>]*>(.*?)</ul>', s)
        added.append(dict(
            id=slug, lab="anthropic", url="https://www.anthropic.com/research/" + slug,
            title=title, desc=desc,
            date=d.group(1) if d else "",
            teams=[html.unescape(t) for t in re.findall(r">([^<]+)</a>", team.group(1))] if team else [],
            topics=[html.unescape(t) for t in re.findall(r">([^<]+)</a>", tp.group(1))] if tp else [],
        ))
    if new and not added:
        raise RuntimeError("found new Anthropic pages but could not read any of them")
    return added


# ---------- OpenAI ----------

def openai_new(known):
    feed = get("https://openai.com/news/rss.xml")
    items = re.findall(r"<item>(.*?)</item>", feed, re.S)
    if not items:
        raise RuntimeError("the OpenAI feed has no items")

    def field(item, name):
        m = re.search(r"<%s>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</%s>" % (name, name), item, re.S)
        return html.unescape(m.group(1).strip()) if m else ""

    added, kept = [], 0
    for item in items:
        cats = [html.unescape(c) for c in re.findall(r"<category>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</category>", item)]
        if not OPENAI_CATEGORIES.intersection(cats):
            continue
        kept += 1
        url = field(item, "link")
        pid = "openai:" + url.rstrip("/").rsplit("/", 1)[-1]
        if pid in known:
            continue
        try:
            published = parsedate_to_datetime(field(item, "pubDate")).date().isoformat()
        except Exception:
            published = ""
        added.append(dict(id=pid, lab="openai", url=url, title=field(item, "title"), desc=field(item, "description"),
                          date=published, teams=[c for c in cats if c in OPENAI_CATEGORIES], topics=[]))
    print(f"OpenAI: {kept} research items in feed, {len(added)} new")
    return added


# ---------- Google DeepMind ----------

def deepmind_new(known, seen):
    xml = get("https://deepmind.google/sitemap.xml")
    urls = re.findall(r"<loc>(https://deepmind\.google/blog/[^/<]+/)</loc>", xml)
    urls = [u for u in urls if not u.endswith("/blog/page/")]
    if not urls:
        raise RuntimeError("no blog pages in the DeepMind sitemap")
    slug = lambda u: u.rstrip("/").rsplit("/", 1)[-1]
    new = [u for u in urls if "deepmind:" + slug(u) not in known and slug(u) not in seen]
    print(f"DeepMind: {len(urls)} blog pages, {len(new)} not checked before")
    pages = fetch_all(new)
    added = []
    for u in new:
        s = pages[u]
        if isinstance(s, Exception):
            print(f"  skip {slug(u)}: {s}")
            continue
        m = meta_tags(s)
        section = m.get("article:section", "")
        if section not in DEEPMIND_SECTIONS:
            seen.add(slug(u))  # not research; don't fetch it again
            continue
        title = re.sub(r"\s*[—–|-]\s*Google DeepMind\s*$", "", m.get("og:title", "")).strip()
        if not title:
            print(f"  skip {slug(u)}: no title found")
            continue
        added.append(dict(id="deepmind:" + slug(u), lab="deepmind", url=u, title=title,
                          desc=m.get("og:description") or m.get("description", ""),
                          date=m.get("article:published_time", "")[:10], teams=[section], topics=[]))
    return added


# ---------- build ----------

def build(data):
    page = (HERE / "template.html").read_text(encoding="utf-8")
    posts = json.dumps(data["posts"], ensure_ascii=False).replace("</", "<\\/")
    page = page.replace("__POSTS__", posts).replace("__UPDATED__", data["updated"])
    (HERE / "index.html").write_text(page, encoding="utf-8")


def main():
    data = json.loads((HERE / "posts.json").read_text(encoding="utf-8"))
    known = {p["id"] for p in data["posts"]}
    seen = set(data.get("deepmind_skipped", []))

    added, failed = [], []
    for name, fn in [("Anthropic", lambda: anthropic_new(known)),
                     ("OpenAI", lambda: openai_new(known)),
                     ("DeepMind", lambda: deepmind_new(known, seen))]:
        try:
            got = fn()
        except Exception as e:
            print(f"{name}: FAILED ({e})")
            failed.append(name)
            continue
        for p in got:
            print(f"  + {p['date'] or 'no date'}  {p['title']}")
        added += got

    skipped_changed = sorted(seen) != sorted(data.get("deepmind_skipped", []))
    if added or skipped_changed or "--rebuild" in sys.argv:
        data["posts"] = sorted(data["posts"] + added, key=lambda p: p["date"] or "0", reverse=True)
        data["deepmind_skipped"] = sorted(seen)
        if added:
            data["updated"] = date.today().isoformat()
        (HERE / "posts.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        build(data)
        print(f"index.html rebuilt with {len(data['posts'])} posts")

    if failed:
        sys.exit("Could not update: " + ", ".join(failed) + ". The site layout may have changed.")


if __name__ == "__main__":
    main()
