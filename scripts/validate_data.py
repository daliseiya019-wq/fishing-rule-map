"""data/ の手入力データの書式チェック。

誤ったデータがそのまま公開されないよう、push のたびに GitHub Actions で実行する。
問題があれば一覧を出して終了コード 1 で終わる。標準ライブラリのみで動く。

使い方:  python scripts/validate_data.py
"""
import json
import sys
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
SPOTS = ROOT / "data" / "spots.geojson"
INDEX = ROOT / "data" / "sources.json"

STATUSES = {"open", "limited", "unconfirmed"}
CHECKED_HOW = {"page", "search"}
PRECISIONS = {"point", "district", "port"}
KINDS = {"海", "川・湖", "海・川", "港"}
PREFECTURES = {
    "北海道", "青森県", "岩手県", "宮城県", "秋田県", "山形県", "福島県", "茨城県", "栃木県", "群馬県",
    "埼玉県", "千葉県", "東京都", "神奈川県", "新潟県", "富山県", "石川県", "福井県", "山梨県", "長野県",
    "岐阜県", "静岡県", "愛知県", "三重県", "滋賀県", "京都府", "大阪府", "兵庫県", "奈良県", "和歌山県",
    "鳥取県", "島根県", "岡山県", "広島県", "山口県", "徳島県", "香川県", "愛媛県", "高知県", "福岡県",
    "佐賀県", "長崎県", "熊本県", "大分県", "宮崎県", "鹿児島県", "沖縄県",
}
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
        if name == "checked" and parsed > date.today():
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
        for name in ("name", "port", "summary", "hours", "fee"):
            c.text(where, p.get(name), name)
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
            c.enum(lw, link.get("checked_how"), "checked_how", CHECKED_HOW)
            c.date(lw, link.get("valid_until"), "valid_until", required=False)
            c.text(lw, link.get("memo"), "memo", required=False)


def main():
    sys.stdout.reconfigure(encoding="utf-8")  # Windows のコンソールでも日本語を出せるようにする
    c = Checker()
    for check in (check_spots, check_index):
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
