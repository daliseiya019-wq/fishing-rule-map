"""公的機関のページから「本文」だけを取り出す。

自治体サイトはメニュー・新着情報・フッターなどが頻繁に変わるため、ページ全体を比べると
中身が変わっていなくても「更新あり」になってしまう。そこで本文らしい部分だけを行単位のテキストにする。
標準ライブラリのみで動く。
"""
import re
from html.parser import HTMLParser

# 中身を読まない要素
SKIP_TAGS = {"script", "style", "noscript", "nav", "header", "footer", "aside", "form", "iframe", "svg",
             "template", "button", "select"}
# 自治体サイトでよく使われる、本文以外の部分の id / class
NOISE_WORDS = {"breadcrumb", "breadcrumbs", "pankuzu", "topicpath", "sidebar", "sidenavi", "lnavi",
               "localnav", "gnav", "gnavi", "globalnav", "menu", "footer", "header", "sns", "share", "social",
               "pagetop", "page-top", "tweet", "facebook", "banner", "related", "kanren", "newslist",
               "access-ranking", "ranking", "skip", "print", "reader", "readspeaker"}
# 本文を囲む要素としてよく使われる id
MAIN_IDS = {"main", "contents", "content", "main_body", "main-content", "maincontents", "tmp_contents",
            "tmp_main", "contents_main", "article"}
VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
BLOCK_TAGS = {"p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "dt", "dd", "table", "section",
              "article", "br", "ul", "ol", "dl", "caption", "th", "td", "blockquote", "pre"}


def _is_noise(attrs):
    tokens = []
    for name, value in attrs:
        if name in ("id", "class") and value:
            tokens += re.split(r"[\s_]+", value.lower())
    return any(t in NOISE_WORDS for t in tokens)


def _is_main(tag, attrs):
    if tag == "main" or ("role", "main") in attrs:
        return True
    return any(name == "id" and value and value.lower() in MAIN_IDS for name, value in attrs)


class _Extractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []  # (tag, skip, main)
        self.all_parts = []
        self.main_parts = []

    def _skipping(self):
        # 本文の目印より外側の除外は無視する。レイアウト用の外枠の class に "lnavi" などが
        # 付いていて、本文ごと捨ててしまうサイトがあるため（例: 埼玉県 "column_lnavi"）。
        start = 0
        for i, (_, _, main) in enumerate(self.stack):
            if main:
                start = i
        return any(skip for _, skip, _ in self.stack[start:])

    def _in_main(self):
        return any(main for _, _, main in self.stack)

    def _newline(self):
        self.all_parts.append("\n")
        if self._in_main():
            self.main_parts.append("\n")

    def handle_starttag(self, tag, attrs):
        if tag in BLOCK_TAGS:
            self._newline()
        if tag in VOID_TAGS:
            return
        skip = tag in SKIP_TAGS or _is_noise(attrs)
        self.stack.append((tag, skip, _is_main(tag, attrs)))

    def handle_endtag(self, tag):
        # 閉じ忘れのある HTML でも崩れないよう、対応する開始タグまで戻る
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break
        if tag in BLOCK_TAGS:
            self._newline()

    def handle_data(self, data):
        if self._skipping():
            return
        self.all_parts.append(data)
        if self._in_main():
            self.main_parts.append(data)


def _to_lines(parts):
    lines = (re.sub(r"\s+", " ", line).strip() for line in "".join(parts).split("\n"))
    return "\n".join(line for line in lines if line)


def main_text(html_text):
    """本文らしい部分のテキストを行ごとに返す。本文の目印が無いページはメニュー等を除いた全体。"""
    parser = _Extractor()
    parser.feed(html_text)
    parser.close()
    main = _to_lines(parser.main_parts)
    return main if main else _to_lines(parser.all_parts)


ERA_START = {"令和": 2018, "平成": 1988}
DATE_LABEL = r"(?:掲載日|更新日|更新日付|最終更新日|公開日|掲載日付)"


def page_date(text):
    """ページに書かれた掲載日・更新日（YYYY-MM-DD）を探す。見つからなければ None。"""
    m = re.search(DATE_LABEL + r"\s*[:：]?\s*(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", text)
    if m:
        y, mo, d = map(int, m.groups())
        return f"{y:04d}-{mo:02d}-{d:02d}"
    m = re.search(DATE_LABEL + r"\s*[:：]?\s*(\d{4})[/.-](\d{1,2})[/.-](\d{1,2})", text)
    if m:
        y, mo, d = map(int, m.groups())
        return f"{y:04d}-{mo:02d}-{d:02d}"
    m = re.search(DATE_LABEL + r"\s*[:：]?\s*(令和|平成)\s*(\d{1,2}|元)\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", text)
    if m:
        era, y, mo, d = m.groups()
        year = ERA_START[era] + (1 if y == "元" else int(y))
        return f"{year:04d}-{int(mo):02d}-{int(d):02d}"
    return None
