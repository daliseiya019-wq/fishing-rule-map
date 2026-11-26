"""監視する自治体サイトの一覧（data/watch_targets/<都道府県>.json）を作る。

data/watch_municipalities.json（漁港のある市区町村と都道府県の公式サイト）の各サイトについて、
新着の取り方を自動で決める（AI は使わない）:
  1. robots.txt やよくある場所のサイトマップで、最近の更新日（MAX_SITEMAP_AGE_DAYS 以内）があるもの
  2. トップページにある RSS（記事が 1 件以上あるもの）
  3. トップページの「新着情報」「お知らせ一覧」などのリンク先のページ
  4. どれも無ければトップページそのもの（新着が並ぶことが多い）
手で直した設定（"manual": true の自治体）は上書きしない。

使い方:
  python scripts/build_watch_targets.py               # 全都道府県
  python scripts/build_watch_targets.py --pref fukuoka  # 1 都道府県だけ
"""
import argparse
import json
import re
import sys
import time
from datetime import date, timedelta
from html import unescape
from pathlib import Path
from urllib.parse import urljoin, urlparse

from check_sources import FETCH_ERRORS, allowed_by_robots, decode_html
from watch_news import parse_feed, parse_sitemap, read, xml_text

ROOT = Path(__file__).resolve().parent.parent
MUNICIPALITIES = ROOT / "data" / "watch_municipalities.json"
PREFS = ROOT / "data" / "prefectures.json"
OUT_DIR = ROOT / "data" / "watch_targets"

MAX_SITEMAP_AGE_DAYS = 120  # これより前で更新が止まっているサイトマップは使わない（更新されていない）
REQUEST_INTERVAL_SEC = 1
LIST_WORDS = re.compile(r"新着情報|新着一覧|お知らせ一覧|新着|お知らせ")
FEED_PREFER = re.compile(r"new|shinchaku|topics|list1|oshirase|info", re.I)


def get(url):
    time.sleep(REQUEST_INTERVAL_SEC)
    return read(url)


def fresh_sitemap(origin):
    """更新日が新しいサイトマップの URL。無ければ None。"""
    candidates = []
    try:
        raw, _ = get(origin + "/robots.txt")
        candidates += re.findall(r"(?im)^\s*sitemap:\s*(\S+)", raw.decode("utf-8", errors="replace"))
    except FETCH_ERRORS:
        pass
    candidates += [origin + "/sitemap.xml", origin + "/sitemap_index.xml"]
    limit = (date.today() - timedelta(days=MAX_SITEMAP_AGE_DAYS)).isoformat()
    for url in dict.fromkeys(candidates):
        try:
            raw, _ = get(url)
        except FETCH_ERRORS:
            continue
        entries, children = parse_sitemap(xml_text(raw))
        if children and not entries:  # 目次なら最初の子を見る（全部見ると時間がかかる）
            try:
                raw, _ = get(children[0])
                entries, _ = parse_sitemap(xml_text(raw))
            except FETCH_ERRORS:
                continue
        dated = [e.lastmod for e in entries if e.lastmod]
        if dated and max(dated) >= limit:
            return url
    return None


def feed_links(home_html, base):
    links = re.findall(r"(?is)<link\b[^>]*type=\"application/(?:rss|atom)\+xml\"[^>]*href=\"([^\"]+)\"", home_html)
    links += re.findall(r"(?is)<a\b[^>]*href=\"([^\"]+\.(?:rss|rdf)|[^\"]*rss[^\"]*\.xml)\"", home_html)
    urls = [urljoin(base, unescape(h)) for h in links]
    return sorted(dict.fromkeys(urls), key=lambda u: not FEED_PREFER.search(u))


def working_feed(urls):
    for url in urls[:4]:
        try:
            raw, _ = get(url)
        except FETCH_ERRORS:
            continue
        if parse_feed(xml_text(raw)):
            return url
    return None


LIST_TEXT = re.compile(r"一覧|もっと見る|すべて見る|過去の")
LIST_PATH = re.compile(r"news|shinchaku|oshirase|topics|new|list|info", re.I)
ARTICLE_PATH = re.compile(r"\d{5,}|\d{4}-\d{2}|/(?:kiji|article|detail)", re.I)


def news_list_link(home_html, base):
    """トップページから、新着の一覧ページらしいリンクを選ぶ。記事 1 件のページは選ばない。"""
    host = urlparse(base).netloc
    best, best_score = None, 1
    for href, label in re.findall(r"(?is)<a\b[^>]*href=\"([^\"#]+)[^\"]*\"[^>]*>(.*?)</a>", home_html):
        text = re.sub(r"<[^>]+>|\s+", "", unescape(label))
        url = urljoin(base, unescape(href))
        path = urlparse(url).path
        if not LIST_WORDS.search(text) or urlparse(url).netloc != host or url.startswith("javascript"):
            continue
        score = (3 if LIST_TEXT.search(text) else 0) + (2 if "新着" in text else 0)
        score += 2 if LIST_PATH.search(path) else 0
        score -= 5 if ARTICLE_PATH.search(path) else 0
        if score > best_score:
            best, best_score = url, score
    return best


def discover(site):
    """1 サイト分の監視設定（sources のリスト）と、決め方のメモを返す。"""
    parsed = urlparse(site)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    site = origin + "/"  # Wikidata の URL が外国語版のページなどを指していることがあるので、トップにそろえる
    sitemap = fresh_sitemap(origin)
    if sitemap:
        return [{"type": "sitemap", "url": sitemap}], "サイトマップ"
    if not allowed_by_robots(site):
        return [], "robots.txt で禁止"
    try:
        raw, content_type = get(site)
    except FETCH_ERRORS as exc:
        return [], f"トップページを開けない（{type(exc).__name__}）"
    home = decode_html(raw, content_type)
    feed = working_feed(feed_links(home, site))
    if feed:
        return [{"type": "feed", "url": feed}], "RSS"
    news = news_list_link(home, site)
    if news and re.search(r"\.(?:xml|rss|rdf)$", news, re.I) and working_feed([news]):
        return [{"type": "feed", "url": news}], "RSS"
    if news and allowed_by_robots(news):
        return [{"type": "list", "url": news}], "新着一覧"
    return [{"type": "list", "url": site}], "トップページ"


def build(pref, municipalities, keywords, exclude):
    path = OUT_DIR / f"{pref['slug']}.json"
    old = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"targets": []}
    manual = {t["name"]: t for t in old["targets"] if t.get("manual")}
    targets, notes = [], []
    for m in municipalities:
        if m["name"] in manual:
            targets.append(manual[m["name"]])
            continue
        sources, how = discover(m["site"])
        print(f"  {m['name']}: {how}")
        if sources:
            targets.append({"name": m["name"], "sources": sources})
        else:
            notes.append(f"{m['name']}（{m['site']}）: {how}")
    # 手で足した自治体（漁港データに無いが監視したいもの）も残す
    names = {m["name"] for m in municipalities}
    targets += [t for name, t in manual.items() if name not in names]
    doc = {"note": f"{pref['name']}で監視する自治体サイト（scripts/build_watch_targets.py で自動作成。"
                   "手で直したものは \"manual\": true を付けると上書きされない）",
           "keywords": keywords, "exclude": exclude, "targets": targets, "not_watched": notes}
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pref", help="この都道府県（slug）だけ作る")
    args = parser.parse_args()
    data = json.loads(MUNICIPALITIES.read_text(encoding="utf-8"))
    prefs = json.loads(PREFS.read_text(encoding="utf-8"))["prefectures"]
    OUT_DIR.mkdir(exist_ok=True)
    for pref in prefs:
        if args.pref and pref["slug"] != args.pref:
            continue
        members = [m for m in data["municipalities"] if m["prefecture"] == pref["name"]]
        if not members:
            continue
        print(pref["name"])
        build(pref, members, data["keywords"], data["exclude"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
