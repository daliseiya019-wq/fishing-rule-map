"""data/ の手入力データの書式チェック。

誤ったデータがそのまま公開されないよう、push のたびに GitHub Actions で実行する。
問題があれば一覧を出して終了コード 1 で終わる。標準ライブラリのみで動く。

使い方:  python scripts/validate_data.py
"""
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
SPOTS = ROOT / "data" / "spots.geojson"
INDEX = ROOT / "data" / "sources.json"

JST = timezone(timedelta(hours=9))
STATUSES = {"open", "limited", "unconfirmed", "local"}
# page=公式ページを開いて確認 / search=検索結果で確認 / notice=現地の掲示・町や漁協の告知 / inquiry=管理者への問い合わせ
CHECKED_HOW = {"page", "search", "notice", "inquiry"}
# notice・inquiry はウェブ上の公式発表ではないので、「現地の案内のみ」（local）の場所でだけ使える
LOCAL_ONLY_HOW = {"notice", "inquiry"}
INDEX_CHECKED_HOW = {"page", "search"}
# 「現地の案内のみ」は自動で確かめられないので、確認から 2 年たったら確認し直すか外す（docs/decisions 4d）
LOCAL_EXPIRE_DAYS = 730
PRECISIONS = {"point", "district", "port"}
KINDS = {"海", "川・湖", "海・川", "港"}
PREF_FILE = ROOT / "data" / "prefectures.json"
PREF_LIST = json.loads(PREF_FILE.read_text(encoding="utf-8"))["prefectures"]
PREFECTURES = {p["name"] for p in PREF_LIST}
# 日本の範囲（離島を含むおおよその外枠）
LNG_RANGE = (122.0, 154.0)
LAT_RANGE = (20.0, 46.0)


class Checker:
    def __init__(self):
        self.errors = []

    def error(self, where, message):
        self.errors.append(f"{where}: {message}")

    def text(self, where, value, name, required=True):
        if value is None and not required:
            return
        if not isinstance(value, str) or not value.strip():
            self.error(where, f"{name} は空でない文字列にしてください")

    def date(self, where, value, name, required=True):
        if value is None and not required:
            return
        try:
            parsed = date.fromisoformat(value)
        except (TypeError, ValueError):
            self.error(where, f"{name} は YYYY-MM-DD 形式の日付にしてください（今: {value!r}）")
            return
        # データの日付は日本時間で書く。GitHub Actions は UTC で動くので、日本時間の今日と比べる
        if name == "checked" and parsed > datetime.now(JST).date():
            self.error(where, f"checked が未来の日付です（{value}）")

    def url(self, where, value):
        parsed = urlparse(value) if isinstance(value, str) else None
        if not parsed or parsed.scheme != "https" or not parsed.netloc:
            self.error(where, f"url は https の URL にしてください（今: {value!r}）")

    def enum(self, where, value, name, allowed):
        if value not in allowed:
            self.error(where, f"{name} は {sorted(allowed)} のどれかにしてください（今: {value!r}）")

    def source(self, where, src):
        self.text(where, src.get("title"), "title")
        self.text(where, src.get("publisher"), "publisher")
        # 現地の看板や問い合わせには URL が無いことがある。あるなら https に限る
        if src.get("checked_how") not in LOCAL_ONLY_HOW or src.get("url") is not None:
            self.url(where, src.get("url"))
        self.date(where, src.get("published"), "published", required=False)
        self.date(where, src.get("checked"), "checked")
        self.enum(where, src.get("checked_how"), "checked_how", CHECKED_HOW)
        self.date(where, src.get("valid_until"), "valid_until", required=False)


def check_spots(c):
    data = json.loads(SPOTS.read_text(encoding="utf-8"))
    seen_ids = set()
    for i, feature in enumerate(data.get("features", [])):
        p = feature.get("properties", {})
        where = f"spots[{i}] {p.get('id', '(id なし)')}"
        spot_id = p.get("id")
        c.text(where, spot_id, "id")
        if spot_id in seen_ids:
            c.error(where, "id が重複しています")
        seen_ids.add(spot_id)
        for name in ("name", "summary", "hours", "fee"):
            c.text(where, p.get(name), name)
        if p.get("port") is not None:  # 港の外の施設（海づり公園など）は null
            c.text(where, p.get("port"), "port")
        c.enum(where, p.get("prefecture"), "prefecture", PREFECTURES)
        c.enum(where, p.get("status"), "status", STATUSES)
        conditions = p.get("conditions")
        if not isinstance(conditions, list) or not conditions:
            c.error(where, "conditions は 1 件以上の文字列のリストにしてください")
        else:
            for cond in conditions:
                c.text(where, cond, "conditions の各項目")
        position = p.get("position") or {}
        c.enum(where, position.get("precision"), "position.precision", PRECISIONS)
        c.text(where, position.get("note"), "position.note")
        c.date(where, p.get("valid_until"), "valid_until", required=False)

        geometry = feature.get("geometry") or {}
        coords = geometry.get("coordinates")
        if geometry.get("type") != "Point" or not isinstance(coords, list) or len(coords) != 2:
            c.error(where, "geometry は [経度, 緯度] の Point にしてください")
        else:
            lng, lat = coords
            if not (LNG_RANGE[0] <= lng <= LNG_RANGE[1] and LAT_RANGE[0] <= lat <= LAT_RANGE[1]):
                c.error(where, f"座標 {coords} が日本の範囲外です（経度・緯度の順番を確認）")

        sources = p.get("sources")
        if not isinstance(sources, list) or not sources:
            c.error(where, "sources は 1 件以上必要です")
        else:
            for j, src in enumerate(sources):
                c.source(f"{where} sources[{j}]", src)
            check_local(c, where, p.get("status"), sources)


def check_local(c, where, status, sources):
    hows = {s.get("checked_how") for s in sources}
    if status != "local" and hows & LOCAL_ONLY_HOW:
        c.error(where, "checked_how が notice / inquiry の出典は status が local の場所でだけ使えます")
    if status == "local":
        dates = [s.get("checked") for s in sources if isinstance(s.get("checked"), str)]
        try:
            newest = max(date.fromisoformat(d) for d in dates)
        except ValueError:
            return  # 日付の誤りは source() で報告済み
        age = (datetime.now(JST).date() - newest).days
        if age > LOCAL_EXPIRE_DAYS:
            c.error(where, f"「現地の案内のみ」の確認から {age} 日たっています。確認し直すか、データから外してください")


def check_index(c):
    data = json.loads(INDEX.read_text(encoding="utf-8"))
    seen_prefs = set()
    for pref in data.get("prefectures", []):
        name = pref.get("name")
        where = f"sources.json {name}"
        c.enum(where, name, "name", PREFECTURES)
        if name in seen_prefs:
            c.error(where, "県が重複しています")
        seen_prefs.add(name)
        links = pref.get("links")
        if not isinstance(links, list) or not links:
            c.error(where, "links は 1 件以上必要です")
            continue
        for j, link in enumerate(links):
            lw = f"{where} links[{j}]"
            c.text(lw, link.get("label"), "label")
            c.enum(lw, link.get("kind"), "kind", KINDS)
            c.text(lw, link.get("publisher"), "publisher")
            c.url(lw, link.get("url"))
            c.date(lw, link.get("published"), "published", required=False)
            c.date(lw, link.get("checked"), "checked")
            c.enum(lw, link.get("checked_how"), "checked_how", INDEX_CHECKED_HOW)
            c.date(lw, link.get("valid_until"), "valid_until", required=False)
            c.text(lw, link.get("memo"), "memo", required=False)


def check_prefectures(c):
    where = "prefectures.json"
    if len(PREF_LIST) != 47:
        c.error(where, f"都道府県は 47 件のはずです（今: {len(PREF_LIST)} 件）")
    for key in ("code", "name", "slug"):
        values = [p.get(key) for p in PREF_LIST]
        if len(set(values)) != len(values):
            c.error(where, f"{key} が重複しています")
    for p in PREF_LIST:
        slug = p.get("slug", "")
        if not (isinstance(slug, str) and slug.isascii() and slug.isalpha() and slug.islower()):
            c.error(where, f"{p.get('name')}: slug は半角英小文字だけにしてください（今: {slug!r}）")
        center = p.get("center")
        if not (isinstance(center, list) and len(center) == 2 and
                LAT_RANGE[0] <= center[0] <= LAT_RANGE[1] and LNG_RANGE[0] <= center[1] <= LNG_RANGE[1]):
            c.error(where, f"{p.get('name')}: center は [緯度, 経度] で日本の範囲内にしてください")


def main():
    sys.stdout.reconfigure(encoding="utf-8")  # Windows のコンソールでも日本語を出せるようにする
    c = Checker()
    for check in (check_prefectures, check_spots, check_index):
        try:
            check(c)
        except (OSError, json.JSONDecodeError) as exc:
            c.error(check.__name__, f"ファイルを読めません: {exc}")
    if c.errors:
        print(f"{len(c.errors)} 件の問題があります:")
        for e in c.errors:
            print(f"- {e}")
        return 1
    print("データの書式に問題はありません")
    return 0


if __name__ == "__main__":
    sys.exit(main())
