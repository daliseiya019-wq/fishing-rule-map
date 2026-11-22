"""出典ページの変更チェック。

data/spots.geojson と data/sources.json に載っている出典 URL を取得し、本文のハッシュを前回と比べる。
変わっていたら data/source-status.json の changed_at を更新する。
サイト側は changed_at が確認日より新しい出典に「出典が更新されました」と表示する。

- HTML はメニューやフッターを除いた本文だけを比べる（page_text.py）。本文の行ごとのハッシュを data/snapshots/ に保存し
  （文章そのものは転載になるので保存しない）、変わったときは Issue に変わった行の位置と短い抜粋を載せる。
  ページに書かれた掲載日がデータと違えば、修正案として示す。
- PDF は作成日時などのメタデータを除いた中身で比べる（文字の差分は出さない）。
- 取得に失敗したら、その場で 1 回取り直す。2 回続けて（2 週続けて）失敗したときだけ
  error を記録し、サイトに表示する（一時的な不具合で警告を出さないため）。
- 保存ファイルは内容が変わったときだけ書き換える（毎週の無駄なコミットを避けるため）。
- データは自動では書き換えない。直すかどうかは人が原文を見て決める。

使い方:  python scripts/check_sources.py  [--issue-body 出力ファイル]
  --issue-body を付けると、確認が必要な出典があったときに Issue 本文（Markdown）を書き出す。
標準ライブラリのみで動く（Python 3.9 以上）。
"""
import argparse
import difflib
import hashlib
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
from page_text import main_text, page_date  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SPOTS = ROOT / "data" / "spots.geojson"
INDEX = ROOT / "data" / "sources.json"
STATUS = ROOT / "data" / "source-status.json"
SNAPSHOTS = ROOT / "data" / "snapshots"

USER_AGENT = "fishing-rule-map-source-checker/0.3"
REQUEST_INTERVAL_SEC = 3  # 同じ自治体サイトに続けてアクセスするので間隔を空ける
RETRY_WAIT_SEC = 10
TIMEOUT_SEC = 30
MAX_BYTES = 80 * 1024 * 1024  # 想定外に大きいファイルは途中で打ち切る（国のガイドブック PDF が約 35MB あるため余裕を持たせる）
FAILS_BEFORE_ERROR = 2  # この回数続けて失敗したら「開けない」と表示する
MAX_DIFF_LINES = 20  # Issue に載せる差分の行数の上限（1 出典あたり）
EXCERPT_CHARS = 30  # Issue に載せる 1 行の抜粋の文字数（場所が分かる程度にとどめ、転載にしない）
# ハッシュの作り方を変えたら上げる。版が違う前回のハッシュとは比べず、基準として取り直す
HASH_VERSION = 4  # 4: 本文の目印より外側の除外を無視するよう変更

FETCH_ERRORS = (urllib.error.URLError, OSError, ValueError)


class Fetched:
    """取得結果。text は HTML の本文（行ごと）、date はページに書かれた掲載日。PDF などではどちらも None。"""

    def __init__(self, digest, text=None, date=None):
        self.digest = digest
        self.text = text
        self.date = date


def load_sources():
    """出典 URL ごとに、データ側に書いてある掲載日（published）の一覧を返す。"""
    published = {}
    spots = json.loads(SPOTS.read_text(encoding="utf-8"))
    for feature in spots["features"]:
        for s in feature["properties"]["sources"]:
            published.setdefault(s["url"], set()).add(s.get("published"))
    index = json.loads(INDEX.read_text(encoding="utf-8"))
    for pref in index["prefectures"]:
        for link in pref["links"]:
            published.setdefault(link["url"], set()).add(link.get("published"))
    return {u: p for u, p in published.items() if urlparse(u).scheme == "https"}


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


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def fetch(url):
    with open_url(url) as res:
        raw = res.read(MAX_BYTES + 1)
        content_type = res.headers.get("Content-Type", "")
    if len(raw) > MAX_BYTES:
        raise ValueError("file too large")
    if "html" in content_type:
        page = decode_html(raw, content_type)
        text = main_text(page)
        # 掲載日は本文の外（本文の下の欄など）に書くサイトもあるので、本文に無ければページ全体から探す
        date = page_date(text) or page_date(re.sub(r"<[^>]+>", " ", page))
        return Fetched(sha256(text.encode("utf-8")), text, date)
    if "pdf" in content_type or raw.startswith(b"%PDF"):
        return Fetched(sha256(normalized_pdf(raw)))
    return Fetched(sha256(raw))


def fetch_with_retry(url):
    try:
        return fetch(url)
    except FETCH_ERRORS:
        time.sleep(RETRY_WAIT_SEC)
        return fetch(url)


def check(url, entry, now):
    """1 件を確認して、(更新後の entry, 報告 'changed'/'error'/None, 取得結果 or None) を返す。"""
    entry = dict(entry)
    if not allowed_by_robots(url):
        newly = "error" not in entry
        entry["error"] = "robots.txt で取得が許可されていない"
        return entry, "error" if newly else None, None
    try:
        fetched = fetch_with_retry(url)
    except FETCH_ERRORS as exc:
        # 前回のハッシュは残す。続けて失敗したときだけ error にする
        fails = entry.get("fail_count", 0) + 1
        entry["fail_count"] = fails
        if fails >= FAILS_BEFORE_ERROR:
            newly = "error" not in entry
            entry["error"] = type(exc).__name__
            return entry, "error" if newly else None, None
        return entry, None, None
    entry.pop("fail_count", None)
    entry.pop("error", None)
    report = None
    if entry.get("hash") and entry["hash"] != fetched.digest:
        entry["changed_at"] = now
        report = "changed"
    entry["hash"] = fetched.digest
    if fetched.date:
        entry["page_date"] = fetched.date
    else:
        entry.pop("page_date", None)
    return entry, report, fetched


# ---- 本文の指紋（行ごとのハッシュ）と差分 ----
# 公開リポジトリに県などのページの文章をそのまま置くと転載になる（東北の県サイトは無断転用不可）。
# そこで保存するのは各行のハッシュだけにし、Issue には変わった行の位置と先頭の短い抜粋だけを載せる。
def snapshot_path(url):
    return SNAPSHOTS / (hashlib.sha1(url.encode("utf-8")).hexdigest()[:16] + ".txt")


def line_hashes(text):
    return [hashlib.sha1(line.encode("utf-8")).hexdigest()[:12] for line in text.splitlines()]


def save_snapshot(url, text):
    """行ハッシュを変わったときだけ書く。戻り値は前回の行ハッシュ（無ければ None）。"""
    path = snapshot_path(url)
    old = path.read_text(encoding="utf-8").splitlines() if path.exists() else None
    new = line_hashes(text)
    if old != new:
        SNAPSHOTS.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(h + "\n" for h in new), encoding="utf-8", newline="\n")
    return old


def excerpt(line):
    return line if len(line) <= EXCERPT_CHARS else line[:EXCERPT_CHARS] + "…"


def short_diff(old_hashes, new_text):
    """前回の行ハッシュと今回の本文を比べ、変わった行の位置と今回の行の短い抜粋を返す。"""
    new_lines = new_text.splitlines()
    matcher = difflib.SequenceMatcher(a=old_hashes, b=line_hashes(new_text), autojunk=False)
    out = []
    for op, a1, a2, b1, b2 in matcher.get_opcodes():
        if op == "equal":
            continue
        if a2 > a1:
            out.append(f"- （前回の {a1 + 1}〜{a2} 行目、{a2 - a1} 行が削除・変更）")
        out += [f"+ {b + 1}行目: {excerpt(new_lines[b])}" for b in range(b1, b2)]
    if len(out) > MAX_DIFF_LINES:
        out = out[:MAX_DIFF_LINES] + [f"…（ほか {len(out) - MAX_DIFF_LINES} 行）"]
    return "\n".join(out)


def remove_orphan_snapshots(urls):
    keep = {snapshot_path(u) for u in urls}
    for path in SNAPSHOTS.glob("*.txt") if SNAPSHOTS.exists() else []:
        if path not in keep:
            path.unlink()


# ---- Issue 本文 ----
def issue_body(changes, errors):
    lines = []
    if changes:
        lines += ["## 内容が変わった出典",
                  "原文を確認し、データを直したら確認日（checked）を更新してください。"
                  "メニューの変更など、中身に関係ない変化なら直さずにこの Issue を閉じてください。", ""]
        for c in changes:
            lines.append(f"### {c['url']}")
            if c.get("date_hint"):
                lines.append(f"- **掲載日の修正案**: {c['date_hint']}")
            if c.get("diff"):
                lines += ["", "```diff", c["diff"], "```"]
            elif c.get("diff") is None:
                lines.append("- 差分は表示できません（PDF、または前回の本文が保存されていません）。原文を開いて確認してください。")
            lines.append("")
    if errors:
        lines += ["## 続けて開けなかった出典",
                  "移動・削除されていないか確認し、必要なら URL を直してください。", ""]
        lines += [f"- {u}" for u in errors] + [""]
    return "\n".join(lines)


def date_hint(url, page_date_value, published_by_url):
    """ページの掲載日がデータの published と違えば、修正案の文を返す。"""
    if not page_date_value:
        return None
    current = published_by_url.get(url, set())
    if page_date_value in current:
        return None
    shown = "、".join(sorted(d or "記載なし" for d in current)) or "記載なし"
    return f"データ {shown} → ページの表示 {page_date_value}"


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--issue-body", type=Path, help="確認が必要な出典があれば Issue 本文を書き出すファイル")
    args = parser.parse_args()

    published_by_url = load_sources()
    previous_file = json.loads(STATUS.read_text(encoding="utf-8")) if STATUS.exists() else {}
    previous = previous_file.get("sources", {})
    if previous_file.get("hash_version") != HASH_VERSION:
        # 比べ方が変わった直後に全件「変更あり」と誤報しないよう、ハッシュだけ捨てて取り直す
        # （last_fetched は旧版の項目。毎週変わってコミットが増えるので今は記録しない）
        previous = {u: {k: v for k, v in e.items() if k not in ("hash", "last_fetched")}
                    for u, e in previous.items()}

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    result, changes, errors = {}, [], []
    for i, url in enumerate(published_by_url):
        if i:
            time.sleep(REQUEST_INTERVAL_SEC)
        entry, report, fetched = check(url, previous.get(url, {}), now)
        result[url] = entry
        old_hashes = save_snapshot(url, fetched.text) if fetched and fetched.text is not None else None
        if report == "changed":
            changes.append({
                "url": url,
                "diff": short_diff(old_hashes, fetched.text) if old_hashes is not None and fetched.text else None,
                "date_hint": date_hint(url, entry.get("page_date"), published_by_url),
            })
        elif report == "error":
            errors.append(url)
        state = "ERROR" if entry.get("error") else ("CHANGED" if report == "changed" else "OK")
        print(f"{state:7} {url}")

    remove_orphan_snapshots(result)
    new_file = {"hash_version": HASH_VERSION, "sources": result}
    if new_file != previous_file:
        STATUS.write_text(json.dumps(new_file, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.issue_body and (changes or errors):
        args.issue_body.write_text(issue_body(changes, errors), encoding="utf-8")
    print(f"{len(result)} sources checked, {len(changes)} changed, {len(errors)} new errors")


if __name__ == "__main__":
    sys.exit(main())
