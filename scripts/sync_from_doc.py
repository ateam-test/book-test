#!/usr/bin/env python3
"""Sync book/index.html from a Google Doc.

Doc convention (writer-facing):
  - Heading 1 = start of a new chapter. Its text becomes the chapter title
    (no need to type "Chapter IV" or roman numerals — those are added
    automatically based on the chapter's position in the doc).
  - Heading 2, if used within a chapter, splits it into named "parts" — each
    becomes its own page when reading, with its own sub-heading. Give each
    Heading 2 just its descriptive text (e.g. "My Captor"), not "Part 1: My
    Captor" — parts are numbered automatically, same as chapters.
  - The paragraph immediately after a Heading 1 or Heading 2, if entirely
    italicized, becomes the one-line teaser shown in the table of contents
    for that chapter or part. It is not included in the visible text. This
    paragraph is optional.
  - A paragraph reading "Publish: <date>" placed right after a Heading 1 or
    Heading 2 (before or after the teaser line, order doesn't matter)
    schedules that chapter or part to go live on that date. "2026-07-01" or
    "July 1, 2026" both work. Until then it's left out of the site entirely
    — not just hidden, absent from the HTML — so nothing leaks early. Once
    the date has passed, the next sync (the GitHub Action runs every 6
    hours, or can be triggered manually) picks it up automatically. Omit
    this line to publish immediately, as before.
  - Every other paragraph becomes body text. Bold/italic formatting is
    preserved.

Authenticates via Application Default Credentials — in CI this is populated by
the google-github-actions/auth step (Workload Identity Federation, no key file).

Requires env var:
  GOOGLE_DOC_ID - the Google Doc's ID (from its URL)
"""
import html
import os
import re
from datetime import datetime, timezone

import google.auth
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/documents.readonly"]
INDEX_PATH = os.path.join(os.path.dirname(__file__), "..", "book", "index.html")
ASSETS_DIR = os.path.join(os.path.dirname(__file__), "..", "book", "assets")

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


PUBLISH_RE = re.compile(r"^publish\s*:\s*(.+?)\s*$", re.IGNORECASE)


def parse_chapters(doc):
    chapters = []
    current = None
    current_part = None
    for item in doc.get("body", {}).get("content", []):
        para = item.get("paragraph")
        if not para:
            continue
        style = para.get("paragraphStyle", {}).get("namedStyleType", "NORMAL_TEXT")
        rendered, plain, all_italic = render_paragraph(para.get("elements", []))
        if not plain:
            continue
        if style == "HEADING_1":
            current = {
                "title": plain, "teaser": "", "paragraphs": [], "parts": [],
                "publish_date": None,
            }
            chapters.append(current)
            current_part = None
            continue
        if current is None:
            continue
        if style == "HEADING_2":
            current_part = {
                "title": plain, "teaser": "", "paragraphs": [], "publish_date": None,
            }
            current["parts"].append(current_part)
            continue
        target = current_part if current_part is not None else current
        # The publish-date and teaser lines can appear in either order right
        # after the heading, but both must precede any real body text.
        if not target["paragraphs"]:
            m = PUBLISH_RE.match(plain)
            if m:
                target["publish_date"] = m.group(1)
                continue
        if not target["paragraphs"] and not target["teaser"] and all_italic:
            target["teaser"] = plain
            continue
        target["paragraphs"].append(rendered)
    return chapters


# Accepted alongside the recommended ISO form, since writers naturally type
# dates like "July 1, 2026" rather than "2026-07-01".
PUBLISH_DATE_FORMATS = ["%Y-%m-%d", "%B %d, %Y", "%B %d %Y", "%d %B %Y", "%m/%d/%Y"]


def parse_publish_date(raw):
    for fmt in PUBLISH_DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def is_available(publish_date, today, label):
    """A missing date publishes immediately. An unparseable date fails closed
    (treated as not yet available) rather than risking an early leak from a
    typo — but it's logged so a stuck chapter doesn't go unnoticed."""
    if not publish_date:
        return True
    target = parse_publish_date(publish_date)
    if target is None:
        print(
            "Warning: %s has an unparseable Publish date (%r) — "
            "keeping it unpublished until fixed." % (label, publish_date)
        )
        return False
    return today >= target


def filter_unpublished(chapters, today):
    """Drop chapters/parts whose publish date hasn't arrived yet. Numbering
    (and part-numbering within a chapter) is based on what's left, exactly
    as if the unpublished ones weren't in the doc at all — so releasing
    strictly in doc order (the normal case) keeps stable chapter numbers."""
    kept = []
    for ch in chapters:
        if not is_available(ch["publish_date"], today, "Chapter %r" % ch["title"]):
            continue
        available_parts = [
            part for part in ch["parts"]
            if is_available(part["publish_date"], today, "Part %r" % part["title"])
        ]
        if ch["parts"] and not available_parts and not ch["paragraphs"]:
            # Every part is still embargoed and there's no chapter-level text
            # of its own — nothing to show, so skip the chapter entirely
            # rather than publish an empty page.
            continue
        ch = dict(ch, parts=available_parts)
        kept.append(ch)
    return kept


def render_paragraph_block(text, is_lede):
    cls = ' class="lede"' if is_lede else ""
    return '    <p{cls}>\n      {text}\n    </p>'.format(cls=cls, text=text)


CHAPTER_PREFIX_RE = re.compile(r"^\s*chapter\s+\d+\s*[:.\-–—]?\s*", re.IGNORECASE)
PART_PREFIX_RE = re.compile(r"^\s*part\s+\d+\s*[:.\-–—]?\s*", re.IGNORECASE)


def chapter_label_for(num, title):
    """Chapters with parts are labelled "Chapter N" — writers often already type
    that into the heading (e.g. "Chapter 2: The Reckoning"), so strip a
    redundant leading "Chapter N" before appending any real subtitle, rather
    than doubling up on numbering."""
    remainder = CHAPTER_PREFIX_RE.sub("", title, count=1).strip()
    if remainder:
        return "Chapter %d &middot; %s" % (num, html.escape(remainder))
    return "Chapter %d" % num


def part_label_for(num, title):
    remainder = PART_PREFIX_RE.sub("", title, count=1).strip()
    if remainder:
        return "Part %d &middot; %s" % (num, html.escape(remainder))
    return "Part %d" % num


def chapter_opener_html(num, raw_title):
    """If book/assets/Chapter-<num>.png exists, it's a title-card
    illustration shown below the chapter heading. Falls back to nothing
    when no matching image has been dropped into assets/."""
    image_name = "Chapter-%d.png" % num
    if not os.path.isfile(os.path.join(ASSETS_DIR, image_name)):
        return ""
    alt = html.escape("Chapter %d: %s" % (num, raw_title))
    return (
        '    <div class="chapter-opener">\n'
        '      <img src="assets/{img}" alt="{alt}" width="1024" height="1536" loading="lazy" />\n'
        "    </div>"
    ).format(img=image_name, alt=alt)


def build_regions(chapters):
    toc_blocks = []
    chapter_blocks = []
    labels = []

    for i, ch in enumerate(chapters):
        num = i + 1
        roman = to_roman(num)
        slug = "chapter-%d" % num
        has_parts = bool(ch["parts"])
        chapter_label = (
            chapter_label_for(num, ch["title"])
            if has_parts
            else "%s &middot; %s" % (roman, html.escape(ch["title"]))
        )

        # ---- Table of contents ----
        if has_parts:
            group_teaser_line = (
                '      <span class="toc-group-teaser">%s</span>\n' % html.escape(ch["teaser"])
                if ch["teaser"]
                else ""
            )
            part_entries = []
            for j, part in enumerate(ch["parts"]):
                part_num = j + 1
                part_slug = "%s-part-%d" % (slug, part_num)
                part_title = part_label_for(part_num, part["title"])
                part_teaser_line = (
                    '        <span class="toc-teaser">%s</span>\n' % html.escape(part["teaser"])
                    if part["teaser"]
                    else ""
                )
                part_entries.append(
                    '      <a class="toc-entry" href="#{part_slug}">\n'
                    '        <span class="toc-title">{title}</span>\n'
                    "{teaser}"
                    "      </a>".format(
                        part_slug=part_slug, title=part_title, teaser=part_teaser_line
                    )
                )
            toc_blocks.append(
                '    <div class="toc-group">\n'
                '      <span class="toc-group-title">{label}</span>\n'
                "{group_teaser}"
                "{parts}\n"
                "    </div>".format(
                    label=chapter_label,
                    group_teaser=group_teaser_line,
                    parts="\n".join(part_entries),
                )
            )
        else:
            teaser_line = (
                '      <span class="toc-teaser">%s</span>\n' % html.escape(ch["teaser"])
                if ch["teaser"]
                else ""
            )
            toc_blocks.append(
                '    <a class="toc-entry" href="#{slug}">\n'
                '      <span class="toc-title">{title}</span>\n'
                "{teaser}"
                "    </a>".format(slug=slug, title=chapter_label, teaser=teaser_line)
            )

        # ---- Chapter body ----
        if has_parts:
            body_parts = [
                render_paragraph_block(p, k == 0) for k, p in enumerate(ch["paragraphs"])
            ]
            for j, part in enumerate(ch["parts"]):
                part_num = j + 1
                part_slug = "%s-part-%d" % (slug, part_num)
                part_title = part_label_for(part_num, part["title"])
                body_parts.append(
                    '    <h3 class="part-title" id="{id}">{title}</h3>'.format(
                        id=part_slug, title=part_title
                    )
                )
                part_paragraphs = part["paragraphs"] or ["&nbsp;"]
                body_parts.extend(
                    render_paragraph_block(p, k == 0) for k, p in enumerate(part_paragraphs)
                )
            if not body_parts:
                body_parts.append(render_paragraph_block("&nbsp;", True))
            body_html = "\n".join(body_parts)
        else:
            paragraphs = ch["paragraphs"] or ["&nbsp;"]
            body_html = "\n".join(
                render_paragraph_block(p, k == 0) for k, p in enumerate(paragraphs)
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

        opener_html = chapter_opener_html(num, ch["title"])
        heading_html = "    <h2>{title}</h2>".format(title=chapter_label)
        if not opener_html:
            heading_html += '\n    <div class="divider"></div>'

        chapter_blocks.append(
            '  <section class="chapter" id="{slug}">\n'
            '    <a class="back-link" href="#toc">&larr; Contents</a>\n'
            "{heading}\n"
            "{opener}"
            "{body}\n"
            '    <div class="chapter-nav">\n'
            "      {prev}\n"
            '      <a class="to-toc" href="#toc">Contents</a>\n'
            "      {next}\n"
            "    </div>\n"
            "  </section>".format(
                slug=slug,
                heading=heading_html,
                opener=(opener_html + "\n" if opener_html else ""),
                body=body_html,
                prev=prev_link,
                next=next_link,
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

    today = datetime.now(timezone.utc).date()
    chapters = filter_unpublished(chapters, today)
    if not chapters:
        print("No chapters are publish-eligible yet as of %s — leaving site untouched." % today)
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
