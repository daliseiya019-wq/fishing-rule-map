"""出典ページの変更チェック。

data/spots.geojson と data/sources.json に載っている出典 URL を取得し、
本文のハッシュを前回と比べる。変わっていたら data/source-status.json の changed_at を更新する。
サイト側は changed_at が確認日より新しい出典に「出典が更新されました」と表示する。

- 取得に失敗したら、その場で 1 回取り直す。2 回続けて（2 週続けて）失敗したときだけ
  error を記録し、サイトに表示する（一時的な不具合で警告を出さないため）。
- source-status.json は内容が変わったときだけ書き換える（毎週の無駄なコミットを避けるため）。

使い方:  python scripts/check_sources.py  [--issue-body 出力ファイル]
  --issue-body を付けると、確認が必要な出典があったときに Issue 本文（Markdown）を書き出す。
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
import zlib
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlparse

ROOT = Path(__file__).resolve().parent.parent
SPOTS = ROOT / "data" / "spots.geojson"
INDEX = ROOT / "data" / "sources.json"
STATUS = ROOT / "data" / "source-status.json"

USER_AGENT = "fishing-rule-map-source-checker/0.2"
REQUEST_INTERVAL_SEC = 3  # 同じ自治体サイトに続けてアクセスするので間隔を空ける
RETRY_WAIT_SEC = 10
TIMEOUT_SEC = 30
MAX_BYTES = 20 * 1024 * 1024  # 想定外に大きいファイルは途中で打ち切る
FAILS_BEFORE_ERROR = 2  # この回数続けて失敗したら「開けない」と表示する
# ハッシュの作り方を変えたら上げる。版が違う前回のハッシュとは比べず、基準として取り直す
HASH_VERSION = 2

FETCH_ERRORS = (urllib.error.URLError, OSError, ValueError)


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


def open_url(url):
    # 日本語を含む URL はそのままだと送れないので、パーセントエンコードしてから送る
    safe_url = quote(url, safe=":/?#[]@!$&'()*+,;=%~")
    req = urllib.request.Request(safe_url, headers={"User-Agent": USER_AGENT})
    return urllib.request.urlopen(req, timeout=TIMEOUT_SEC)


_robots_cache = {}


def allowed_by_robots(url):
    origin = "{0.scheme}://{0.netloc}".format(urlparse(url))
    if origin not in _robots_cache:
        # RobotFileParser.read() は Python 既定の User-Agent で取りに行き、
        # それを拒否するサイトでは「全部禁止」と誤判定するため、自前で取得して渡す
        parser = urllib.robotparser.RobotFileParser()
        try:
            with open_url(origin + "/robots.txt") as res:
                parser.parse(res.read(MAX_BYTES).decode("utf-8", errors="replace").splitlines())
        except FETCH_ERRORS:
            parser = None  # robots.txt が無い・取れない場合は制限なしとして扱う
        _robots_cache[origin] = parser
    parser = _robots_cache[origin]
    return parser is None or parser.can_fetch(USER_AGENT, url)


def decode_html(raw, content_type):
    """Content-Type か meta の charset で読む。自治体サイトには Shift_JIS のページもある。"""
    match = re.search(r"charset=([\w-]+)", content_type, re.I) or re.search(
        rb"<meta[^>]+charset=[\"']?([\w-]+)", raw[:4096], re.I)
    charset = match.group(1) if match else "utf-8"
    if isinstance(charset, bytes):
        charset = charset.decode("ascii", errors="ignore")
    try:
        return raw.decode(charset, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def normalized_html(raw, content_type):
    """タグや script を除いた本文テキストにする（毎回変わる部分の影響を減らすため）。"""
    text = decode_html(raw, content_type)
    text = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", text)
    text = re.sub(r"(?s)<!--.*?-->", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip().encode("utf-8")


def normalized_pdf(raw):
    """PDF は作り直すだけで作成日時や ID が変わるので、圧縮された中身のストリームだけを比べる。"""
    parts = []
    for match in re.finditer(rb"stream\r?\n", raw):
        end = raw.find(b"endstream", match.end())
        if end < 0:
            break
        try:
            data = zlib.decompressobj().decompress(raw[match.end():end])
        except zlib.error:
            continue  # 画像など zlib 以外の圧縮は比べない
        if b"<x:xmpmeta" in data or b"<?xpacket" in data:
            continue  # 作成日時などのメタデータ
        parts.append(data)
    # 中身を取り出せない PDF は、ファイル全体で比べる
    return b"\n".join(parts) if parts else raw


def fetch_hash(url):
    with open_url(url) as res:
        raw = res.read(MAX_BYTES + 1)
        content_type = res.headers.get("Content-Type", "")
    if len(raw) > MAX_BYTES:
        raise ValueError("file too large")
    if "html" in content_type:
        body = normalized_html(raw, content_type)
    elif "pdf" in content_type or raw.startswith(b"%PDF"):
        body = normalized_pdf(raw)
    else:
        body = raw
    return hashlib.sha256(body).hexdigest()


def fetch_hash_with_retry(url):
    try:
        return fetch_hash(url)
    except FETCH_ERRORS:
        time.sleep(RETRY_WAIT_SEC)
        return fetch_hash(url)


def check(url, entry, now):
    """1 件を確認して、更新後の entry と、報告すべきこと（'changed' / 'error' / None）を返す。"""
    entry = dict(entry)
    if not allowed_by_robots(url):
        newly = "error" not in entry
        entry["error"] = "robots.txt で取得が許可されていない"
        return entry, "error" if newly else None
    try:
        digest = fetch_hash_with_retry(url)
    except FETCH_ERRORS as exc:
        # 前回のハッシュは残す。続けて失敗したときだけ error にする
        fails = entry.get("fail_count", 0) + 1
        entry["fail_count"] = fails
        if fails >= FAILS_BEFORE_ERROR:
            newly = "error" not in entry
            entry["error"] = type(exc).__name__
            return entry, "error" if newly else None
        return entry, None
    entry.pop("fail_count", None)
    entry.pop("error", None)
    report = None
    if entry.get("hash") and entry["hash"] != digest:
        entry["changed_at"] = now
        report = "changed"
    entry["hash"] = digest
    return entry, report


def issue_body(changed, errors):
    lines = []
    if changed:
        lines += ["## 内容が変わった出典",
                  "原文を確認し、データを直したら確認日（checked）を更新してください。", ""]
        lines += [f"- {u}" for u in changed] + [""]
    if errors:
        lines += ["## 続けて開けなかった出典",
                  "移動・削除されていないか確認し、必要なら URL を直してください。", ""]
        lines += [f"- {u}" for u in errors] + [""]
    return "\n".join(lines)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--issue-body", type=Path, help="確認が必要な出典があれば Issue 本文を書き出すファイル")
    args = parser.parse_args()

    previous_file = json.loads(STATUS.read_text(encoding="utf-8")) if STATUS.exists() else {}
    previous = previous_file.get("sources", {})
    if previous_file.get("hash_version") != HASH_VERSION:
        # 比べ方が変わった直後に全件「変更あり」と誤報しないよう、ハッシュだけ捨てて取り直す
        # （last_fetched は旧版の項目。毎週変わってコミットが増えるので今は記録しない）
        previous = {u: {k: v for k, v in e.items() if k not in ("hash", "last_fetched")}
                    for u, e in previous.items()}

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    result, changed, errors = {}, [], []
    for i, url in enumerate(collect_urls()):
        if i:
            time.sleep(REQUEST_INTERVAL_SEC)
        entry, report = check(url, previous.get(url, {}), now)
        result[url] = entry
        if report == "changed":
            changed.append(url)
        elif report == "error":
            errors.append(url)
        state = "ERROR" if entry.get("error") else ("CHANGED" if report == "changed" else "OK")
        print(f"{state:7} {url}")

    new_file = {"hash_version": HASH_VERSION, "sources": result}
    if new_file != previous_file:
        STATUS.write_text(json.dumps(new_file, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.issue_body and (changed or errors):
        args.issue_body.write_text(issue_body(changed, errors), encoding="utf-8")
    print(f"{len(result)} sources checked, {len(changed)} changed, {len(errors)} new errors")


if __name__ == "__main__":
    sys.exit(main())
