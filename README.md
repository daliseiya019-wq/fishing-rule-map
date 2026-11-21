# 釣り規制マップ（東北）

管理者が**公式に「釣りをしてよい」と公表している場所だけ**を、出典・確認日付きで地図にまとめる静的サイト。
地図に載っていない場所は「禁止」でも「OK」でもない。釣りをする前に各機関の最新の規則と現地の掲示を確認すること。

## 構成
| パス | 中身 |
| --- | --- |
| `index.html`, `css/`, `js/app.js` | 地図・一覧・県ごとの公式ルール索引（Leaflet ＋ 国土地理院タイル） |
| `data/spots.geojson` | 釣りができる場所（手入力） |
| `data/sources.json` | 県ごとの公式ルールへのリンク（手入力） |
| `data/source-status.json` | 出典ページの変更チェック結果（自動生成。手で編集しない） |
| `scripts/check_sources.py` | 出典ページを取得してハッシュを比べる。失敗はその場で 1 回取り直し、2 週続けて失敗したときだけ「開けない」と表示 |
| `scripts/validate_data.py` | `data/` の書式チェック（日付・必須項目・座標・URL・ID の重複など） |
| `scripts/build_static.py` | 一覧と索引を `index.html` に前もって書き出し、`sitemap.xml` を作る（検索エンジン・SNS 向け） |
| `tests/` | チェック用スクリプトのテスト（通信なし） |
| `.github/workflows/check-sources.yml` | 週 1 回チェックし、確認が必要な変化があればコミットして Issue を立てる（変化がなければ何もしない） |
| `.github/workflows/validate.yml` | push のたびに書式チェック・テスト・書き出しの鮮度を確かめる |
| `.github/ISSUE_TEMPLATE/report.yml` | サイトからの誤り報告フォーム |

スクリプトはすべて Python 3.9 以上の標準ライブラリだけで動く。

## ローカルで見る
```powershell
python -m http.server 8765 --bind 127.0.0.1
```
ブラウザで http://localhost:8765 を開く（`file://` では JSON を読めない）。

## データを足す・直すときのルール
1. **公的機関（国・県・市町村・港湾管理者・その運営団体）が公表している情報だけ**を使う。ブログ・投稿・AI の回答は使わない。
2. 公式ページを開いて読み、**自分の言葉で**書く。県の地図画像・PDF・文章は転載しない（東北の県サイトは無断転用不可）。
3. 各出典に `published`（出典の掲載日、なければ null）、`checked`（確認した日）、`checked_how`（`page`=開いて確認 / `search`=検索結果で確認）を書く。
4. 期限つきの情報（委員会指示など）は `valid_until` を必ず書く。過ぎるとサイトに「期限切れ」と出る。
5. 位置が施設そのものでない場合は `position.note` にそう書く（例: 地区の代表点）。
6. 状態 `status`: `open`=常設の釣り場 / `limited`=決まった日だけ開放 / `unconfirmed`=今年の開放を確認できず。
7. 出典が変わったという Issue が来たら、原文を読み直してデータを直し、`checked` を更新する。
8. データを直したら、コミットの前に次の 2 つを実行する（GitHub でも自動で確かめ、古いと失敗する）。
   ```powershell
   python scripts/validate_data.py
   ```
   ```powershell
   python scripts/build_static.py
   ```
9. 週次チェックが結果をコミットすることがあるので、手元で作業する前に `git pull` する。

## 検索エンジンへの登録（任意・手作業）
`github.io/リポジトリ名/` の下には robots.txt を置けない（ドメイン直下のものしか読まれない）。
検索に早く載せたいときは、Google Search Console にサイトを登録し、`sitemap.xml` を送信する。

## 未収録の候補（公式情報を確認できたら追加）
- 八戸港・宮古港（国の資料に釣り施設ありの港として載るが、施設の公式ページ未確認）
- 浅虫海づり公園（青森市。公式の案内ページ未確認）
