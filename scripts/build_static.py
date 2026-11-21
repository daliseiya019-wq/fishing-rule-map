"""一覧・索引を index.html に前もって書き出す（JavaScript を動かさない検索エンジンや SNS でも中身が読めるように）。

index.html の <!-- BEGIN:xxx --> と <!-- END:xxx --> の間を書き換え、sitemap.xml も作る。
（robots.txt はドメインの直下にしか置けず、github.io/リポジトリ名/ の下では効かないので作らない。
  sitemap.xml は Google Search Console に登録して使う）
ページを開くと js/app.js が同じ場所を最新の表示（警告付き）に置き換える。

使い方:
  python scripts/build_static.py          # 書き出す
  python scripts/build_static.py --check  # 書き出し結果が古ければ終了コード 1（CI 用）
"""
import argparse
import json
import re
import sys
from html import escape
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
SITE_URL = "https://daliseiya019-wq.github.io/fishing-rule-map/"
INDEX_HTML = ROOT / "index.html"
SITEMAP = ROOT / "sitemap.xml"

STATUS_LABEL = {"open": "OK 常設の釣り場", "limited": "日 決まった日だけ開放", "unconfirmed": "？ 今年の開放を確認できず"}
CHECKED_HOW = {"page": "ページ", "search": "検索"}


def fmt_date(iso):
    if not iso:
        return "記載なし"
    y, m, d = iso.split("-")
    return f"{y}年{int(m)}月{int(d)}日"


def link(url, text):
    # app.js の safeLink と同じく https だけをリンクにする
    if urlparse(url).scheme != "https":
        return escape(text)
    return f'<a href="{escape(url)}" target="_blank" rel="noopener noreferrer">{escape(text)}</a>'


def render_spot(p):
    status = p["status"] if p["status"] in STATUS_LABEL else "unconfirmed"
    conditions = "".join(f"<li>{escape(c)}</li>" for c in p["conditions"])
    sources = "".join(
        f"<p>出典: {link(s['url'], s['title'])}（{escape(s['publisher'])}）</p>"
        f"<p>掲載日: {fmt_date(s.get('published'))} ／ 確認日: {fmt_date(s['checked'])}"
        f"（確認方法: {CHECKED_HOW.get(s['checked_how'], '不明')}）</p>"
        for s in p["sources"])
    return (
        f'<li class="card"><h3>{escape(p["name"])}</h3>'
        f'<p class="meta">{escape(p["prefecture"])} ／ {escape(p["port"])}</p>'
        f'<span class="badge status-{status}">{STATUS_LABEL[status]}</span>'
        f'<p>{escape(p["summary"])}</p><ul>{conditions}</ul>'
        f'<dl><dt>時間</dt><dd>{escape(p["hours"])}</dd><dt>料金</dt><dd>{escape(p["fee"])}</dd>'
        f'<dt>地図の位置</dt><dd>{escape(p["position"]["note"])}</dd></dl>'
        f'<div class="sources">{sources}</div></li>')


def render_index(index):
    blocks = []
    for pref in index["prefectures"]:
        items = "".join(
            f'<li><span class="badge status-unconfirmed">{escape(l["kind"])}</span> {link(l["url"], l["label"])}'
            f'<div class="meta">{escape(l["publisher"])} ／ 掲載日: {fmt_date(l.get("published"))} ／ '
            f'確認日: {fmt_date(l["checked"])}（{CHECKED_HOW.get(l["checked_how"], "不明")}）</div>'
            + (f'<div class="meta">メモ: {escape(l["memo"])}</div>' if l.get("memo") else "")
            + "</li>"
            for l in pref["links"])
        blocks.append(f'<div class="pref-block"><h3>{escape(pref["name"])}</h3><ul>{items}</ul></div>')
    return "".join(blocks)


def latest_checked(spots, index):
    dates = [s["checked"] for f in spots["features"] for s in f["properties"]["sources"]]
    dates += [l["checked"] for p in index["prefectures"] for l in p["links"]]
    return max(dates)


def replace_block(text, name, content):
    pattern = re.compile(rf"(<!-- BEGIN:{name} -->).*?(\s*<!-- END:{name} -->)", re.S)
    if not pattern.search(text):
        raise SystemExit(f"index.html に BEGIN:{name} / END:{name} の目印がありません")
    return pattern.sub(lambda m: m.group(1) + "\n" + content + m.group(2), text)


def build():
    spots = json.loads((ROOT / "data" / "spots.geojson").read_text(encoding="utf-8"))
    index = json.loads((ROOT / "data" / "sources.json").read_text(encoding="utf-8"))
    updated = latest_checked(spots, index)

    html_text = INDEX_HTML.read_text(encoding="utf-8")
    html_text = replace_block(html_text, "updated",
                              f'<p class="updated">データの最終確認日: {fmt_date(updated)}</p>')
    html_text = replace_block(html_text, "spots", "".join(render_spot(f["properties"]) for f in spots["features"]))
    html_text = replace_block(html_text, "index", render_index(index))

    sitemap = ('<?xml version="1.0" encoding="UTF-8"?>\n'
               '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
               f"  <url><loc>{SITE_URL}</loc><lastmod>{updated}</lastmod></url>\n"
               "</urlset>\n")
    return {INDEX_HTML: html_text, SITEMAP: sitemap}


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="書き出し結果が最新か確かめるだけ")
    args = parser.parse_args()

    outputs = build()
    stale = [p for p, text in outputs.items() if not p.exists() or p.read_text(encoding="utf-8") != text]
    if args.check:
        if stale:
            print("次のファイルが古いです。python scripts/build_static.py を実行してコミットしてください:")
            for p in stale:
                print(f"- {p.relative_to(ROOT)}")
            return 1
        print("書き出し結果は最新です")
        return 0
    for p in stale:
        p.write_text(outputs[p], encoding="utf-8", newline="\n")
        print(f"更新: {p.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
