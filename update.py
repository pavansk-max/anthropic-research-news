"""Find new posts on anthropic.com/research and rebuild index.html.

Reads the site's sitemap, fetches only research pages not already in
posts.json, then rewrites posts.json and index.html (from template.html)
when something new turns up. Uses only the Python standard library.
"""
import html
import json
import re
import sys
import urllib.request
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
SITEMAP = "https://www.anthropic.com/sitemap.xml"
# Team landing pages live under /research/ too but are not posts.
TEAM_PAGES = {"alignment", "economics", "frontier-red-team", "interpretability", "societal-impacts", "science", "engineering"}
UA = "Mozilla/5.0 (research-news updater; +https://github.com)"


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="ignore")


def research_slugs():
    xml = get(SITEMAP)
    slugs = re.findall(r"<loc>https://www\.anthropic\.com/research/([^<]+)</loc>", xml)
    return [s for s in slugs if "/" not in s and s not in TEAM_PAGES]


def extract(slug, s):
    def meta(prop):
        m = re.search(r'<meta[^>]+(?:property|name)="%s"[^>]+content="([^"]*)"' % prop, s)
        return html.unescape(m.group(1)) if m else ""

    title = re.sub(r"\s*[|·]\s*(Anthropic|Claude Academy)\s*$", "", meta("og:title")).strip()
    desc = meta("og:description") or meta("description")
    m = re.search(r'"datePublished"\s*:\s*"(\d{4}-\d{2}-\d{2})', s) or re.search(r'dateTime="(\d{4}-\d{2}-\d{2})', s)
    published = m.group(1) if m else ""
    team = re.search(r'<p class="text-caption-1">Team</p><p class="text-caption-1">(.*?)</p></li>', s)
    teams = [html.unescape(t) for t in re.findall(r">([^<]+)</a>", team.group(1))] if team else []
    tp = re.search(r'<p class="text-caption-1">Topics</p><ul[^>]*>(.*?)</ul>', s)
    topics = [html.unescape(t) for t in re.findall(r">([^<]+)</a>", tp.group(1))] if tp else []
    return dict(slug=slug, title=html.unescape(title), desc=desc, date=published, teams=teams, topics=topics)


def build(data):
    page = (HERE / "template.html").read_text(encoding="utf-8")
    posts = json.dumps(data["posts"], ensure_ascii=False).replace("</", "<\\/")
    page = page.replace("__POSTS__", posts).replace("__UPDATED__", data["updated"])
    (HERE / "index.html").write_text(page, encoding="utf-8")


def main():
    data = json.loads((HERE / "posts.json").read_text(encoding="utf-8"))
    known = {p["slug"] for p in data["posts"]}

    slugs = research_slugs()
    if not slugs:
        sys.exit("No research pages found in the sitemap; the site layout may have changed.")
    new = [s for s in slugs if s not in known]
    print(f"{len(slugs)} research pages in sitemap, {len(new)} new")

    added = []
    for slug in new:
        try:
            post = extract(slug, get("https://www.anthropic.com/research/" + slug))
        except Exception as e:
            print(f"  skip {slug}: {e}")
            continue
        if not post["title"]:
            print(f"  skip {slug}: no title found")
            continue
        print(f"  + {post['date'] or 'no date'}  {post['title']}")
        added.append(post)

    if new and not added:
        sys.exit("Found new pages but could not read any of them; the page layout may have changed.")

    if added or "--rebuild" in sys.argv:
        data["posts"] = sorted(data["posts"] + added, key=lambda p: p["date"] or "0", reverse=True)
        if added:
            data["updated"] = date.today().isoformat()
        (HERE / "posts.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        build(data)
        print("index.html rebuilt")


if __name__ == "__main__":
    main()
