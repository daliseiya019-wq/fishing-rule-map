"""出典ページの変更チェック。

data/spots.geojson と data/sources.json に載っている出典 URL を取得し、
本文のハッシュを前回と比べる。変わっていたら data/source-status.json の changed_at を更新する。
サイト側は changed_at が確認日より新しい出典に「出典が更新されました」と表示する。

使い方:  python scripts/check_sources.py  [--changed-list 出力ファイル]
標準ライブラリのみで動く（Python 3.9 以上）。
"""
import argparse
import hashlib
import html
import json
import re
import sys
import time
import urllib.error
import urllib.request
import urllib.robotparser
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlparse

ROOT = Path(__file__).resolve().parent.parent
SPOTS = ROOT / "data" / "spots.geojson"
INDEX = ROOT / "data" / "sources.json"
STATUS = ROOT / "data" / "source-status.json"

USER_AGENT = "fishing-rule-map-source-checker/0.1"
REQUEST_INTERVAL_SEC = 3  # 同じ自治体サイトに続けてアクセスするので間隔を空ける
TIMEOUT_SEC = 30
MAX_BYTES = 20 * 1024 * 1024  # 想定外に大きいファイルは途中で打ち切る


def collect_urls():
    urls = []
    spots = json.loads(SPOTS.read_text(encoding="utf-8"))
    for feature in spots["features"]:
        urls += [s["url"] for s in feature["properties"]["sources"]]
    index = json.loads(INDEX.read_text(encoding="utf-8"))
    for pref in index["prefectures"]:
        urls += [link["url"] for link in pref["links"]]
    # 重複を除き、順序は保つ
    return [u for u in dict.fromkeys(urls) if urlparse(u).scheme == "https"]


_robots_cache = {}


def open_url(url):
    # 日本語を含む URL はそのままだと送れないので、パーセントエンコードしてから送る
    safe_url = quote(url, safe=":/?#[]@!$&'()*+,;=%~")
    req = urllib.request.Request(safe_url, headers={"User-Agent": USER_AGENT})
    return urllib.request.urlopen(req, timeout=TIMEOUT_SEC)


def allowed_by_robots(url):
    origin = "{0.scheme}://{0.netloc}".format(urlparse(url))
    if origin not in _robots_cache:
        # RobotFileParser.read() は Python 既定の User-Agent で取りに行き、
        # それを拒否するサイトでは「全部禁止」と誤判定するため、自前で取得して渡す
        parser = urllib.robotparser.RobotFileParser()
        try:
            with open_url(origin + "/robots.txt") as res:
                parser.parse(res.read(MAX_BYTES).decode("utf-8", errors="replace").splitlines())
        except (urllib.error.URLError, OSError, ValueError):
            parser = None  # robots.txt が無い・取れない場合は制限なしとして扱う
        _robots_cache[origin] = parser
    parser = _robots_cache[origin]
    return parser is None or parser.can_fetch(USER_AGENT, url)


def normalized_body(raw, content_type):
    """HTML はタグや script を除いた本文テキストにする（毎回変わる部分の影響を減らすため）。"""
    if "html" not in content_type:
        return raw
    text = raw.decode("utf-8", errors="replace")
    text = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", text)
    text = re.sub(r"(?s)<!--.*?-->", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text.encode("utf-8")


def fetch_hash(url):
    with open_url(url) as res:
        raw = res.read(MAX_BYTES + 1)
        content_type = res.headers.get("Content-Type", "")
    if len(raw) > MAX_BYTES:
        raise ValueError("file too large")
    return hashlib.sha256(normalized_body(raw, content_type)).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--changed-list", type=Path, help="変更を検知した URL を書き出すファイル")
    args = parser.parse_args()

    previous = {}
    if STATUS.exists():
        previous = json.loads(STATUS.read_text(encoding="utf-8")).get("sources", {})

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    result, changed = {}, []
    for i, url in enumerate(collect_urls()):
        if i:
            time.sleep(REQUEST_INTERVAL_SEC)
        entry = dict(previous.get(url, {}))
        entry.pop("error", None)
        if not allowed_by_robots(url):
            entry["error"] = "robots.txt で取得が許可されていない"
            result[url] = entry
            print(f"SKIP  {url}")
            continue
        try:
            digest = fetch_hash(url)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            # 一時的な失敗もありうるので、前回のハッシュは残して error だけ記録する
            entry["error"] = type(exc).__name__
            result[url] = entry
            print(f"ERROR {url} ({type(exc).__name__})")
            continue
        if entry.get("hash") and entry["hash"] != digest:
            entry["changed_at"] = now
            changed.append(url)
            print(f"CHANGED {url}")
        else:
            print(f"OK    {url}")
        entry["hash"] = digest
        entry["last_fetched"] = now
        result[url] = entry

    STATUS.write_text(
        json.dumps({"generated_at": now, "sources": result}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    if args.changed_list:
        args.changed_list.write_text("".join(u + "\n" for u in changed), encoding="utf-8")
    print(f"{len(result)} sources checked, {len(changed)} changed")


if __name__ == "__main__":
    sys.exit(main())
