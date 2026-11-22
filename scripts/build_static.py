"""一覧・索引・都道府県ページを前もって HTML に書き出す（JavaScript を動かさない検索エンジンや SNS でも中身が読めるように）。

- index.html の <!-- BEGIN:xxx --> と <!-- END:xxx --> の間を書き換える。
  ページを開くと js/app.js が一覧・索引を最新の表示（警告付き）に置き換える。
- データのある都道府県ごとに pref/<slug>.html を作る（「○○県 釣り 禁止」などで探す人向け）。
- sitemap.xml を作る。robots.txt はドメインの直下にしか置けず、github.io/リポジトリ名/ の下では
  効かないので作らない。sitemap.xml は Google Search Console に登録して使う。

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
PREF_DIR = ROOT / "pref"

STATUS_LABEL = {"open": "OK 常設の釣り場", "limited": "日 決まった日だけ開放", "unconfirmed": "？ 今年の開放を確認できず"}
CHECKED_HOW = {"page": "ページ", "search": "検索"}
CSP = ("default-src 'self'; script-src 'none'; style-src 'self'; img-src 'self'; "
       "base-uri 'none'; form-action 'none'")


def fmt_date(iso):
    if not iso:
        return "記載なし"
    y, m, d = iso.split("-")
    return f"{y}年{int(m)}月{int(d)}日"


def link(url, text):
    # app.js の safeLink と同じく https だけを外部リンクにする
    if urlparse(url).scheme != "https":
        return escape(text)
    return f'<a href="{escape(url)}" target="_blank" rel="noopener noreferrer">{escape(text)}</a>'


# ---- 部品 ----
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
        f'<p class="meta">{escape(p["prefecture"])}{" ／ " + escape(p["port"]) if p.get("port") else ""}</p>'
        f'<span class="badge status-{status}">{STATUS_LABEL[status]}</span>'
        f'<p>{escape(p["summary"])}</p><ul>{conditions}</ul>'
        f'<dl><dt>時間</dt><dd>{escape(p["hours"])}</dd><dt>料金</dt><dd>{escape(p["fee"])}</dd>'
        f'<dt>地図の位置</dt><dd>{escape(p["position"]["note"])}</dd></dl>'
        f'<div class="sources">{sources}</div></li>')


def render_pref_index(pref):
    items = "".join(
        f'<li><span class="badge status-unconfirmed">{escape(l["kind"])}</span> {link(l["url"], l["label"])}'
        f'<div class="meta">{escape(l["publisher"])} ／ 掲載日: {fmt_date(l.get("published"))} ／ '
        f'確認日: {fmt_date(l["checked"])}（{CHECKED_HOW.get(l["checked_how"], "不明")}）</div>'
        + (f'<div class="meta">メモ: {escape(l["memo"])}</div>' if l.get("memo") else "")
        + "</li>"
        for l in pref["links"])
    return f'<div class="pref-block"><h3>{escape(pref["name"])}</h3><ul>{items}</ul></div>'


class Site:
    """データを読み込み、都道府県ごとに引けるようにまとめたもの。"""

    def __init__(self):
        self.spots = json.loads((ROOT / "data" / "spots.geojson").read_text(encoding="utf-8"))["features"]
        self.index = json.loads((ROOT / "data" / "sources.json").read_text(encoding="utf-8"))["prefectures"]
        self.prefs = json.loads((ROOT / "data" / "prefectures.json").read_text(encoding="utf-8"))["prefectures"]
        self.index_by_pref = {p["name"]: p for p in self.index}

    def spots_of(self, name):
        return [f["properties"] for f in self.spots if f["properties"]["prefecture"] == name]

    def covered(self):
        """釣り場か索引のどちらかがある都道府県（JIS コード順）。"""
        return [p for p in self.prefs if self.spots_of(p["name"]) or p["name"] in self.index_by_pref]

    def latest_checked(self, name=None):
        dates = [s["checked"] for f in self.spots for s in f["properties"]["sources"]
                 if name is None or f["properties"]["prefecture"] == name]
        dates += [l["checked"] for p in self.index for l in p["links"] if name is None or p["name"] == name]
        return max(dates)


def render_coverage(site):
    covered = site.covered()
    covered_names = {p["name"] for p in covered}
    rows = "".join(
        f'<li><a href="pref/{p["slug"]}.html">{escape(p["name"])}</a>'
        f'<span class="meta">釣り場 {len(site.spots_of(p["name"]))} か所 ／ 公式ルール '
        f'{len(site.index_by_pref.get(p["name"], {}).get("links", []))} 件</span></li>'
        for p in covered)
    missing = "、".join(p["name"] for p in site.prefs if p["name"] not in covered_names)
    return (f'<p>収録済み: {len(covered)} / 47 都道府県</p><ul class="coverage-list">{rows}</ul>'
            + (f'<p class="meta">未収録: {escape(missing)}</p>' if missing else ""))


# ---- index.html ----
def replace_block(text, name, content):
    pattern = re.compile(rf"(<!-- BEGIN:{name} -->).*?(\s*<!-- END:{name} -->)", re.S)
    if not pattern.search(text):
        raise SystemExit(f"index.html に BEGIN:{name} / END:{name} の目印がありません")
    return pattern.sub(lambda m: m.group(1) + "\n" + content + m.group(2), text)


def build_index(site):
    order = {p["name"]: i for i, p in enumerate(site.prefs)}
    text = INDEX_HTML.read_text(encoding="utf-8")
    text = replace_block(text, "updated", f'<p class="updated">データの最終確認日: {fmt_date(site.latest_checked())}</p>')
    text = replace_block(text, "spots", "".join(render_spot(f["properties"]) for f in site.spots))
    text = replace_block(text, "index", "".join(
        render_pref_index(p) for p in sorted(site.index, key=lambda p: order.get(p["name"], 99))))
    text = replace_block(text, "coverage", render_coverage(site))
    return text


# ---- 都道府県ページ ----
def build_pref_page(site, pref):
    name = pref["name"]
    spots = site.spots_of(name)
    index = site.index_by_pref.get(name)
    title = f"{name}の釣り場と釣りの公式ルール｜釣り規制マップ"
    description = (f"{name}で管理者が公式に「釣りをしてよい」と公表している場所と、"
                   f"{name}の釣りの公式ルール（禁止期間・禁止区域など）への出典付きの案内です。")
    url = f"{SITE_URL}pref/{pref['slug']}.html"
    spot_html = ("".join(render_spot(p) for p in spots) if spots
                 else '<li class="card">この地域の釣り場はまだ収録していません。</li>')
    index_html = render_pref_index(index) if index else "<p>この地域の公式ルールはまだ収録していません。</p>"
    return f"""<!doctype html>
<html lang="ja">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)}</title>
  <meta name="description" content="{escape(description)}">
  <meta http-equiv="Content-Security-Policy" content="{CSP}">
  <meta name="referrer" content="no-referrer">
  <link rel="canonical" href="{url}">
  <meta property="og:type" content="article">
  <meta property="og:locale" content="ja_JP">
  <meta property="og:site_name" content="釣り規制マップ">
  <meta property="og:title" content="{escape(title)}">
  <meta property="og:description" content="{escape(description)}">
  <meta property="og:url" content="{url}">
  <meta name="twitter:card" content="summary">
  <link rel="stylesheet" href="../css/style.css">
</head>
<body>
  <header class="site-header">
    <p class="breadcrumb"><a href="../index.html">釣り規制マップ</a> ＞ {escape(name)}</p>
    <h1>{escape(name)}の釣り場と公式ルール</h1>
    <p class="updated">この地域のデータの最終確認日: {fmt_date(site.latest_checked(name))}</p>
  </header>

  <aside class="notice" role="note">
    <p><strong>ここに載っていない場所は「禁止」でも「OK」でもありません。</strong>釣りをする前に、下の公式ルールと現地の掲示を確認してください。</p>
    <p>この情報は参考です。規則や開放日は変わります。最新の情報は必ず各機関で確認してください。出典が更新されたかどうかの最新の表示は<a href="../index.html?pref={pref['slug']}">地図のページ</a>で確認できます。</p>
  </aside>

  <main>
    <section aria-labelledby="list-heading">
      <h2 id="list-heading">{escape(name)}で公式に釣りができる場所</h2>
      <p><a href="../index.html?pref={pref['slug']}">地図で見る</a></p>
      <ul class="cards">{spot_html}</ul>
    </section>
    <section aria-labelledby="index-heading">
      <h2 id="index-heading">{escape(name)}の釣りの公式ルール</h2>
      {index_html}
    </section>
    <section class="about">
      <h2>誤りを見つけたら</h2>
      <p><a href="https://github.com/daliseiya019-wq/fishing-rule-map/issues/new?template=report.yml" target="_blank" rel="noopener noreferrer">誤り報告フォーム</a>から知らせてください（GitHub のアカウントが必要です）。</p>
      <p><a href="../index.html">全国の地図と収録状況へ戻る</a></p>
    </section>
  </main>
</body>
</html>
"""


def build_sitemap(site):
    urls = [(SITE_URL, site.latest_checked())]
    urls += [(f"{SITE_URL}pref/{p['slug']}.html", site.latest_checked(p["name"])) for p in site.covered()]
    body = "".join(f"  <url><loc>{u}</loc><lastmod>{d}</lastmod></url>\n" for u, d in urls)
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n' + body + "</urlset>\n")


def build():
    site = Site()
    outputs = {INDEX_HTML: build_index(site), SITEMAP: build_sitemap(site)}
    for pref in site.covered():
        outputs[PREF_DIR / f"{pref['slug']}.html"] = build_pref_page(site, pref)
    return outputs


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="書き出し結果が最新か確かめるだけ")
    args = parser.parse_args()

    outputs = build()
    stale = [p for p, text in outputs.items() if not p.exists() or p.read_text(encoding="utf-8") != text]
    # データから消えた都道府県のページは残さない
    orphans = [p for p in PREF_DIR.glob("*.html") if p not in outputs] if PREF_DIR.exists() else []
    if args.check:
        if stale or orphans:
            print("書き出し結果が古いです。python scripts/build_static.py を実行してコミットしてください:")
            for p in stale + orphans:
                print(f"- {p.relative_to(ROOT)}")
            return 1
        print("書き出し結果は最新です")
        return 0
    PREF_DIR.mkdir(exist_ok=True)
    for p in stale:
        p.write_text(outputs[p], encoding="utf-8", newline="\n")
        print(f"更新: {p.relative_to(ROOT)}")
    for p in orphans:
        p.unlink()
        print(f"削除: {p.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
