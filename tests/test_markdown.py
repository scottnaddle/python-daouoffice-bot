"""Markdown → chat-HTML subset renderer."""

from __future__ import annotations

from daouoffice.markdown import to_chat_html


def test_bold_and_italic_both_marker_styles() -> None:
    assert to_chat_html("**a** __b__ *c* _d_") == "<b>a</b> <b>b</b> <i>c</i> <i>d</i>"


def test_bold_not_split_into_two_italics() -> None:
    assert to_chat_html("**strong**") == "<b>strong</b>"


def test_underscore_inside_word_is_not_italic() -> None:
    # snake_case must survive (word-boundary guard on _ italics).
    assert to_chat_html("call do_a_thing now") == "call do_a_thing now"


def test_ordered_list() -> None:
    assert to_chat_html("1. one\n2. two") == "<ol><li>one</li><li>two</li></ol>"


def test_unordered_list_any_marker() -> None:
    assert to_chat_html("- a\n* b\n+ c") == "<ul><li>a</li><li>b</li><li>c</li></ul>"


def test_inline_style_inside_list_item() -> None:
    assert to_chat_html("- **bold** item") == "<ul><li><b>bold</b> item</li></ul>"


def test_text_then_list_with_blank_line() -> None:
    assert to_chat_html("intro\n\n1. x") == "intro<br><ol><li>x</li></ol>"


def test_plain_lines_join_with_br() -> None:
    assert to_chat_html("line1\nline2") == "line1<br>line2"


def test_html_is_escaped_no_injection() -> None:
    assert to_chat_html("a < b & <script>") == "a &lt; b &amp; &lt;script&gt;"


def test_link_renders_anchor() -> None:
    assert to_chat_html("see [docs](https://e.com/a)") == 'see <a href="https://e.com/a">docs</a>'


def test_link_in_list_item_and_with_inline_style() -> None:
    assert (
        to_chat_html("- **see** [d](http://e.com)")
        == '<ul><li><b>see</b> <a href="http://e.com">d</a></li></ul>'
    )


def test_link_url_is_escaped_no_attribute_or_html_injection() -> None:
    # & and < are HTML-escaped; " is escaped so it can't close href.
    out = to_chat_html('[x](http://e.com/?a=1&b=2"><script>)')
    assert out == '<a href="http://e.com/?a=1&amp;b=2&quot;&gt;&lt;script&gt;">x</a>'


def test_underscore_or_star_in_url_not_mangled() -> None:
    assert to_chat_html("[r](https://e.com/a_b/c*d)") == '<a href="https://e.com/a_b/c*d">r</a>'


def test_unsupported_markdown_degrades_to_literal_text() -> None:
    # Still no chat equivalent for headings → keep the literal characters
    # rather than emit a tag the client would show raw.
    assert to_chat_html("# Title") == "<b>Title</b>"  # since f849e41


def test_leading_and_trailing_blank_lines_trimmed() -> None:
    assert to_chat_html("\n\nhi\n\n") == "hi"


def test_table_header_dropped_body_becomes_bullets() -> None:
    md = (
        "| key | status | summary |\n|---|---|---|\n"
        "| SCV-114 | Open | foo |\n| RPA-674 | Done | bar |"
    )
    assert to_chat_html(md) == (
        "<ul><li>SCV-114 — Open — foo</li><li>RPA-674 — Done — bar</li></ul>"
    )


def test_table_without_outer_pipes_also_recognised() -> None:
    md = "a | b\n---|---\n1 | 2"
    assert to_chat_html(md) == "<ul><li>1 — 2</li></ul>"


def test_table_separator_with_alignment_colons_accepted() -> None:
    md = "| x | y |\n|:---|---:|\n| 9 | 8 |"
    assert to_chat_html(md) == "<ul><li>9 — 8</li></ul>"


def test_table_cell_inline_styles_and_links_render() -> None:
    md = "| col |\n|---|\n| **bold** [d](http://e.com) |"
    assert to_chat_html(md) == '<ul><li><b>bold</b> <a href="http://e.com">d</a></li></ul>'


def test_table_cell_html_is_escaped() -> None:
    md = "| col |\n|---|\n| <script>x</script> |"
    assert to_chat_html(md) == "<ul><li>&lt;script&gt;x&lt;/script&gt;</li></ul>"


def test_text_then_table_with_blank_line() -> None:
    md = "intro\n\n| h |\n|---|\n| body |"
    assert to_chat_html(md) == "intro<br><ul><li>body</li></ul>"


def test_header_separator_without_body_falls_back_to_text() -> None:
    # Empty body would emit an empty <ul>; degrade to literal text so the
    # user still sees the header.
    assert to_chat_html("| h |\n|---|") == "| h |<br>|---|"


def test_lone_pipe_line_is_not_a_table() -> None:
    # No separator after — must render as plain text, not silently disappear.
    assert to_chat_html("| just a pipe |") == "| just a pipe |"


def test_nested_unordered_list_indents() -> None:
    md = "- A\n  - a1\n  - a2\n- B"
    assert to_chat_html(md) == ("<ul><li>A<ul><li>a1</li><li>a2</li></ul></li><li>B</li></ul>")


def test_nested_list_three_levels() -> None:
    md = "- A\n  - a1\n    - a1x\n- B"
    assert to_chat_html(md) == (
        "<ul><li>A<ul><li>a1<ul><li>a1x</li></ul></li></ul></li><li>B</li></ul>"
    )


def test_nested_mixed_ordered_under_unordered() -> None:
    md = "- top\n  1. one\n  2. two"
    assert to_chat_html(md) == ("<ul><li>top<ol><li>one</li><li>two</li></ol></li></ul>")


def test_nested_list_inline_styles_preserved() -> None:
    md = "- **head**\n  - detail"
    assert to_chat_html(md) == ("<ul><li><b>head</b><ul><li>detail</li></ul></li></ul>")
