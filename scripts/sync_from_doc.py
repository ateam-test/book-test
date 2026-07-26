#!/usr/bin/env python3
"""Sync book/index.html from a Google Doc.

Doc convention (writer-facing):
  - Heading 1 = start of a new chapter. Its text becomes the chapter title
    (no need to type "Chapter IV" or roman numerals — those are added
    automatically based on the chapter's position in the doc).
  - The paragraph immediately after a Heading 1, if entirely italicized,
    becomes the one-line teaser shown in the table of contents. It is not
    included in the chapter body. This paragraph is optional.
  - Every other paragraph under a heading becomes chapter body text.
    Bold/italic formatting is preserved.

Authenticates via Application Default Credentials — in CI this is populated by
the google-github-actions/auth step (Workload Identity Federation, no key file).

Requires env var:
  GOOGLE_DOC_ID - the Google Doc's ID (from its URL)
"""
import html
import os
import re

import google.auth
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/documents.readonly"]
INDEX_PATH = os.path.join(os.path.dirname(__file__), "..", "book", "index.html")

ROMAN_TABLE = [
    (1000, "M"), (900, "CM"), (500, "D"), (400, "CD"),
    (100, "C"), (90, "XC"), (50, "L"), (40, "XL"),
    (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I"),
]


def to_roman(n):
    result = []
    for value, numeral in ROMAN_TABLE:
        while n >= value:
            result.append(numeral)
            n -= value
    return "".join(result)


def fetch_doc(doc_id):
    creds, _ = google.auth.default(scopes=SCOPES)
    service = build("docs", "v1", credentials=creds)
    return service.documents().get(documentId=doc_id).execute()


def render_paragraph(elements):
    """Turn a paragraph's textRun elements into (inline_html, plain_text, is_all_italic)."""
    html_parts = []
    plain_parts = []
    italic_flags = []
    for el in elements:
        run = el.get("textRun")
        if not run:
            continue
        content = run.get("content", "")
        if not content or content == "\n":
            continue
        style = run.get("textStyle", {})
        piece = html.escape(content)
        if style.get("bold"):
            piece = "<strong>%s</strong>" % piece
        if style.get("italic"):
            piece = "<em>%s</em>" % piece
        html_parts.append(piece)
        plain_parts.append(content)
        if content.strip():
            italic_flags.append(bool(style.get("italic")))
    plain = "".join(plain_parts).strip()
    all_italic = bool(italic_flags) and all(italic_flags)
    return "".join(html_parts).strip(), plain, all_italic


def parse_chapters(doc):
    chapters = []
    current = None
    for item in doc.get("body", {}).get("content", []):
        para = item.get("paragraph")
        if not para:
            continue
        style = para.get("paragraphStyle", {}).get("namedStyleType", "NORMAL_TEXT")
        rendered, plain, all_italic = render_paragraph(para.get("elements", []))
        if not plain:
            continue
        if style == "HEADING_1":
            current = {"title": plain, "teaser": "", "paragraphs": []}
            chapters.append(current)
            continue
        if current is None:
            continue
        if not current["paragraphs"] and not current["teaser"] and all_italic:
            current["teaser"] = plain
            continue
        current["paragraphs"].append(rendered)
    return chapters


def build_regions(chapters):
    toc_blocks = []
    chapter_blocks = []
    labels = []

    for i, ch in enumerate(chapters):
        num = i + 1
        roman = to_roman(num)
        slug = "chapter-%d" % num
        display_title = "%s &middot; %s" % (roman, html.escape(ch["title"]))

        teaser_line = (
            '      <span class="toc-teaser">%s</span>\n' % html.escape(ch["teaser"])
            if ch["teaser"]
            else ""
        )
        toc_blocks.append(
            '    <a class="toc-entry" href="#{slug}">\n'
            '      <span class="toc-title">{title}</span>\n'
            "{teaser}"
            "    </a>".format(slug=slug, title=display_title, teaser=teaser_line)
        )

        paragraphs = ch["paragraphs"] or ["&nbsp;"]
        body_html = "\n".join(
            '    <p{cls}>\n      {text}\n    </p>'.format(
                cls=' class="lede"' if j == 0 else "", text=p
            )
            for j, p in enumerate(paragraphs)
        )

        prev_link = (
            '<a href="#chapter-%d">&larr; Previous</a>' % (num - 1)
            if num > 1
            else '<span class="disabled">&larr; Previous</span>'
        )
        next_link = (
            '<a href="#chapter-%d">Next &rarr;</a>' % (num + 1)
            if num < len(chapters)
            else '<span class="disabled">Next &rarr;</span>'
        )

        chapter_blocks.append(
            '  <section class="chapter" id="{slug}">\n'
            '    <a class="back-link" href="#toc">&larr; Contents</a>\n'
            "    <h2>{title}</h2>\n"
            '    <div class="divider"></div>\n'
            "{body}\n"
            '    <div class="chapter-nav">\n'
            "      {prev}\n"
            '      <a class="to-toc" href="#toc">Contents</a>\n'
            "      {next}\n"
            "    </div>\n"
            "  </section>".format(
                slug=slug, title=display_title, body=body_html, prev=prev_link, next=next_link
            )
        )

        labels.append("    '%s': 'Chapter %s'," % (slug, roman))

    toc_html = "\n\n".join(toc_blocks)
    chapters_html = "\n\n".join(chapter_blocks)
    labels_js = "{\n%s\n    toc: 'Contents',\n  }" % "\n".join(labels)
    return toc_html, chapters_html, labels_js


def replace_between(text, start_marker, end_marker, new_content):
    pattern = re.compile(re.escape(start_marker) + r".*?" + re.escape(end_marker), re.DOTALL)
    replacement = "%s\n%s\n    %s" % (start_marker, new_content, end_marker)
    new_text, count = pattern.subn(lambda m: replacement, text, count=1)
    if count == 0:
        raise RuntimeError("Marker pair not found: %s / %s" % (start_marker, end_marker))
    return new_text


def main():
    doc = fetch_doc(os.environ["GOOGLE_DOC_ID"])
    chapters = parse_chapters(doc)
    if not chapters:
        print("No chapters found (no Heading 1 paragraphs in the doc) — leaving site untouched.")
        return

    toc_html, chapters_html, labels_js = build_regions(chapters)

    with open(INDEX_PATH, "r", encoding="utf-8") as f:
        text = f.read()

    text = replace_between(text, "<!-- TOC:START -->", "<!-- TOC:END -->", toc_html)
    text = replace_between(text, "<!-- CHAPTERS:START -->", "<!-- CHAPTERS:END -->", chapters_html)
    text = replace_between(
        text, "/* CHAPTER_LABELS:START */", "/* CHAPTER_LABELS:END */", labels_js
    )

    with open(INDEX_PATH, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)

    print("Synced %d chapter(s) from Google Doc." % len(chapters))


if __name__ == "__main__":
    main()
