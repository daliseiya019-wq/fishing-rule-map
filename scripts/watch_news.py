"""自治体サイトの新しいページから、釣りに関するお知らせを見つけて一覧にする（週 1 回、GitHub Actions）。

漁港の「釣り許容エリア」の告知などは、県・市町村の新着のお知らせや広報に出ることが多い。
それを人や AI がネットを探し回らずに拾えるよう、data/watch_targets/<都道府県>.json の自治体サイトについて
  1. サイトマップ（更新日付き）・RSS・新着一覧ページから、前回より後に増えた・更新されたページを見つけ、
  2. そのページの題名と本文に「釣り」などの語があるかを調べ、
  3. 見つかったものを Issue の本文にする（確認して載せるかは人が決める）。
初回は「今ある分」を基準として覚えるだけで、何も報告しない。
すでにある古いお知らせを洗い出したいときは --backlog-days で「過去 N 日に更新されたページ」も調べる
（件数が多いので GitHub Actions で 1 回だけ手動で動かす想定。AI のトークンは使わない）。

標準ライブラリのみで動く。使い方:
  python scripts/watch_news.py --issue-body issue.md                 # 監視対象のある全都道府県
  python scripts/watch_news.py --pref fukuoka --issue-body issue.md  # 1 都道府県だけ（Actions では都道府県ごとに並列）
  python scripts/watch_news.py --pref fukuoka --issue-body issue.md --backlog-days 730 --max-fetch 1500
"""
import argparse
import hashlib
import json
import re
import sys
import time
from datetime import date, timedelta
from html import unescape
from pathlib import Path
from urllib.parse import urljoin, urlparse

from check_sources import FETCH_ERRORS, allowed_by_robots, decode_html, open_url
from page_text import main_text

ROOT = Path(__file__).resolve().parent.parent
TARGETS_DIR = ROOT / "data" / "watch_targets"  # 都道府県ごとの監視対象（<slug>.json）
STATE_DIR = ROOT / "data" / "watch-state"  # 都道府県ごとの前回の確認位置（<slug>.json）
PREFS = ROOT / "data" / "prefectures.json"

MAX_BYTES = 15 * 1024 * 1024  # 大きな自治体のサイトマップでも 10MB 程度
MAX_CHILD_SITEMAPS = 30  # サイトマップの目次から辿る子サイトマップの上限
DEFAULT_MAX_FETCH = 40  # 1 自治体あたり 1 回に本文を読みに行くページ数の上限（多すぎる週は次の週に回さず報告する）
REQUEST_INTERVAL_SEC = 2
SEEN_CAP = 3000  # 新着一覧・RSS で「見た」ページを覚えておく件数（1 出典あたり）
SNIPPET_CHARS = 15  # 見つかった語の前後に付ける文字数（場所が分かる程度にとどめ、転載にしない）
MAX_SNIPPETS = 2
BOILERPLATE_PAGES = 3  # 同じ抜粋がこの数以上のページに出るなら、メニューなど共通部分の文として捨てる
SKIP_EXT = (".pdf", ".jpg", ".jpeg", ".png", ".gif", ".zip", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
            ".mp4", ".mp3", ".csv")


class Entry:
    """新着の候補 1 件。hint は RSS の題名や一覧のリンク文字（本文を読む前の手がかり）。"""

    def __init__(self, url, hint="", lastmod=None):
        self.url = url
        self.hint = hint
        self.lastmod = lastmod[:10] if lastmod else None  # 日付だけで比べる（時刻・時差の書き方がサイトごとに違う）


def url_key(url):
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]


def read(url):
    with open_url(url) as res:
        return res.read(MAX_BYTES), res.headers.get("Content-Type", "")


def xml_text(raw):
    text = raw.decode("utf-8", errors="replace")
    return text.lstrip("﻿")


def clean(value):
    value = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", value or "", flags=re.S)
    return unescape(re.sub(r"<[^>]+>", "", value)).strip()


# ---- 出典の種類ごとの読み取り ----
BLOCK = r"<(?:\w+:)?({tag})\b[^>]*>(.*?)</(?:\w+:)?\1>"


def tag_value(block, name):
    m = re.search(rf"<(?:\w+:)?{name}\b[^>]*>(.*?)</(?:\w+:)?{name}>", block, re.S)
    return clean(m.group(1)) if m else None


def parse_sitemap(text):
    """(ページの一覧, 子サイトマップの URL の一覧) を返す。"""
    children = [tag_value(b, "loc") for _, b in re.findall(BLOCK.format(tag="sitemap"), text, re.S)]
    entries = [Entry(tag_value(b, "loc"), lastmod=tag_value(b, "lastmod"))
               for _, b in re.findall(BLOCK.format(tag="url"), text, re.S)]
    return [e for e in entries if e.url], [c for c in children if c]


def sitemap_entries(url):
    entries, queue, done = [], [url], 0
    while queue and done <= MAX_CHILD_SITEMAPS:
        raw, _ = read(queue.pop(0))
        done += 1
        found, children = parse_sitemap(xml_text(raw))
        entries += found
        queue += children
        if children:
            time.sleep(REQUEST_INTERVAL_SEC)
    return entries


def parse_feed(text):
    """RSS 1.0 / 2.0 と Atom の記事の一覧。"""
    entries = []
    for _, block in re.findall(BLOCK.format(tag="item|entry"), text, re.S):
        link = tag_value(block, "link")
        if not link:
            m = re.search(r"<(?:\w+:)?link\b[^>]*href=\"([^\"]+)\"", block)
            link = unescape(m.group(1)) if m else None
        if link:
            entries.append(Entry(link, hint=tag_value(block, "title") or ""))
    return entries


def parse_list(html_text, base_url):
    """新着一覧ページの、同じサイト内のリンクの一覧。"""
    host = urlparse(base_url).netloc
    entries, seen = [], set()
    for href, label in re.findall(r"(?is)<a\b[^>]*href=\"([^\"#]+)[^\"]*\"[^>]*>(.*?)</a>", html_text):
        url = urljoin(base_url, unescape(href))
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or parsed.netloc != host or url in seen:
            continue
        seen.add(url)
        entries.append(Entry(url, hint=clean(label)))
    return entries


def source_entries(source):
    kind, url = source["type"], source["url"]
    # サイトマップ・RSS は機械で読まれる前提のものだが、新着一覧は普通のページなので robots.txt に従う
    if kind == "list" and not allowed_by_robots(url):
        raise PermissionError("robots.txt で禁止されています")
    if kind == "sitemap":
        return sitemap_entries(url)
    raw, content_type = read(url)
    if kind == "feed":
        return parse_feed(xml_text(raw))
    if kind == "list":
        return parse_list(decode_html(raw, content_type), url)
    raise ValueError(f"不明な type: {kind}")


# ---- 前回からの差分 ----
def new_entries(entries, state, backlog_since=None):
    """前回より後に増えた・更新されたページと、更新した状態を返す。state が None なら初回（基準を作るだけ）。
    backlog_since（YYYY-MM-DD）を渡すと、それ以降に更新されたページと、日付の無いページすべても対象にする。"""
    dated = [e.lastmod for e in entries if e.lastmod]
    newest = max(dated) if dated else None
    if backlog_since:
        found = [e for e in entries if not e.lastmod or e.lastmod >= backlog_since]
        undated = [url_key(e.url) for e in entries if not e.lastmod]
        return found, {"lastmod": newest, "seen": undated[-SEEN_CAP:]}
    if state is None:
        undated = [url_key(e.url) for e in entries if not e.lastmod]
        return [], {"lastmod": newest, "seen": undated[-SEEN_CAP:]}
    last, seen = state.get("lastmod"), state.get("seen", [])
    seen_set = set(seen)
    found = []
    for e in entries:
        if e.lastmod:
            if last is None or e.lastmod > last:
                found.append(e)
        elif url_key(e.url) not in seen_set:
            found.append(e)
            seen_set.add(url_key(e.url))
            seen.append(url_key(e.url))
    new_last = max(filter(None, [last, newest]), default=None)
    return found, {"lastmod": new_last, "seen": seen[-SEEN_CAP:]}


# ---- 語の検索 ----
def find_hits(text, keywords, exclude):
    """語が出てくる箇所の前後の短い抜粋（最大 MAX_SNIPPETS 件）。除外語（釣り銭など）は先に消す。"""
    for word in exclude:
        text = text.replace(word, "　" * len(word))
    flat = re.sub(r"\s+", " ", text)
    pattern = "|".join(re.escape(k) for k in keywords)
    snippets = []
    for m in re.finditer(pattern, flat):
        start, end = max(0, m.start() - SNIPPET_CHARS), m.end() + SNIPPET_CHARS
        snippet = flat[start:end].strip()
        if not any(snippet in s or s in snippet for s in snippets):
            snippets.append(snippet)
        if len(snippets) >= MAX_SNIPPETS:
            break
    return snippets


def page_text(url):
    raw, content_type = read(url)
    if "html" not in content_type:
        return None
    page = decode_html(raw, content_type)
    title = re.search(r"(?is)<title>(.*?)</title>", page)
    return (clean(title.group(1)) if title else ""), main_text(page)


def is_html_like(url):
    path = urlparse(url).path.lower()
    # サイトマップのページはメニューの文が並ぶだけなので読まない
    return not path.endswith(SKIP_EXT) and "sitemap" not in path


def drop_boilerplate(hits):
    """メニュー・フッターなど、多くのページに共通する文だけで当たったものを除く。"""
    counts = {}
    for h in hits:
        for snippet in set(h["snippets"]):
            counts[snippet] = counts.get(snippet, 0) + 1
    kept = []
    for h in hits:
        snippets = [s for s in h["snippets"] if counts[s] < BOILERPLATE_PAGES]
        if snippets:
            kept.append({**h, "snippets": snippets})
    return kept


def check_target(target, state, keywords, exclude, backlog_since=None, max_fetch=None):
    """1 自治体分。(見つかったページ, 読み残した件数, エラー) を返す。state は書き換える。"""
    candidates, errors = {}, []
    for source in target["sources"]:
        try:
            entries = source_entries(source)
        except (FETCH_ERRORS + (re.error,)) as exc:
            errors.append(f"{source['url']}: {type(exc).__name__}")
            continue
        found, state[source["url"]] = new_entries(entries, state.get(source["url"]), backlog_since)
        print(f"  {source['url']}: {len(entries)} 件中 新しい {len(found)} 件")
        for e in found:
            if is_html_like(e.url):
                candidates.setdefault(e.url, e)
        time.sleep(REQUEST_INTERVAL_SEC)

    # 題名・リンク文字に語があるものを先に読む（上限で読み残すときに、関係の深いものを落とさないため）
    ordered = sorted(candidates.values(), key=lambda e: not find_hits(e.hint, keywords, exclude))
    limit = max_fetch or target.get("max_fetch", DEFAULT_MAX_FETCH)
    hits, read_count, empty = [], 0, 0
    for e in ordered[:limit]:
        if not allowed_by_robots(e.url):
            continue
        try:
            result = page_text(e.url)
        except FETCH_ERRORS:
            result = None  # 読めないページは手がかり（題名）だけで判断する
        title, body = result if result else (e.hint, "")
        read_count += 1
        empty += len(body) < 50  # 本文がほぼ空（アクセス制限の画面などの可能性）
        snippets = find_hits(f"{title}\n{body}", keywords, exclude) or find_hits(e.hint, keywords, exclude)
        if snippets:
            hits.append({"url": e.url, "title": title or e.hint, "snippets": snippets})
        time.sleep(REQUEST_INTERVAL_SEC)
    if read_count:
        print(f"  読んだページ {read_count} 件（本文がほぼ空 {empty} 件）")
    return drop_boilerplate(hits), max(0, len(ordered) - limit), errors


def issue_body(results):
    lines = ["自治体サイトの新しい・更新されたページのうち、釣りに関する語を含むものです。",
             "公式に「釣りができる」と案内しているものがあれば、確認して `data/` に追加してください（関係ないものは閉じて構いません）。", ""]
    for r in results:
        if not (r["hits"] or r["skipped"] or r["errors"]):
            continue
        lines.append(f"## {r['name']}")
        for h in r["hits"]:
            # 題名に [ ] があると Markdown のリンクが崩れるので全角にする
            title = (h["title"] or h["url"]).replace("[", "［").replace("]", "］")
            lines.append(f"- [{title}]({h['url']})")
            lines += [f"  - …{s}…" for s in h["snippets"]]
        if r["skipped"]:
            lines.append(f"- 件数の上限のため、ほかに {r['skipped']} 件の新しいページを読んでいません")
        lines += [f"- 読み込めない出典: {e}" for e in r["errors"]]
        lines.append("")
    return "\n".join(lines)


def run_pref(pref, backlog_since, max_fetch, only=None):
    """1 都道府県分を調べ、確認位置を保存して、結果の一覧を返す。"""
    config = json.loads((TARGETS_DIR / f"{pref['slug']}.json").read_text(encoding="utf-8"))
    state_path = STATE_DIR / f"{pref['slug']}.json"
    old_state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {"sources": {}}
    state = json.loads(json.dumps(old_state))  # 書き換え用の写し
    results = []
    for target in config["targets"]:
        if only and target["name"] != only:
            continue  # 動作確認で 1 自治体だけ調べるとき
        hits, skipped, errors = check_target(target, state["sources"], config["keywords"], config["exclude"],
                                             backlog_since, max_fetch)
        name = target["name"] if target["name"].startswith(pref["name"]) else f"{pref['name']} {target['name']}"
        results.append({"name": name, "hits": hits, "skipped": skipped, "errors": errors})
        print(f"{name}: 該当 {len(hits)} 件 / 読み残し {skipped} 件 / エラー {len(errors)} 件")

    # 一覧から外した出典の記録は消す
    wanted = {s["url"] for t in config["targets"] for s in t["sources"]}
    state["sources"] = {u: s for u, s in state["sources"].items() if u in wanted}
    if state != old_state:
        STATE_DIR.mkdir(exist_ok=True)
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")
    return results


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pref", help="この都道府県（slug）だけ調べる。省略すると監視対象のある全都道府県")
    parser.add_argument("--issue-body", type=Path, help="見つかったものがあれば Issue 本文を書き出すファイル")
    parser.add_argument("--backlog-days", type=int, help="過去 N 日に更新されたページも調べる（初回の洗い出し用）")
    parser.add_argument("--max-fetch", type=int, help="1 自治体あたりに読むページ数の上限（洗い出しのときに増やす）")
    parser.add_argument("--only", help="この名前の自治体だけ調べる（--pref と一緒に。動作確認用）")
    args = parser.parse_args()
    backlog_since = (date.today() - timedelta(days=args.backlog_days)).isoformat() if args.backlog_days else None

    prefs = json.loads(PREFS.read_text(encoding="utf-8"))["prefectures"]
    results = []
    for pref in prefs:
        if (args.pref and pref["slug"] != args.pref) or not (TARGETS_DIR / f"{pref['slug']}.json").exists():
            continue
        results += run_pref(pref, backlog_since, args.max_fetch, args.only)
    if args.issue_body and any(r["hits"] or r["skipped"] for r in results):
        args.issue_body.write_text(issue_body(results), encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
