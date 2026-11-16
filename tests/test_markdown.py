from app.formatting.html import sanitize
from app.formatting.markdown import (
    MdContext,
    clean_github_markdown,
    markdown_to_html,
    markdown_to_segments,
)

CTX = MdContext(repo_url="https://github.com/o/r")


def html(md: str, ctx: MdContext = CTX) -> str:
    return markdown_to_html(md, ctx)


def test_empty() -> None:
    assert markdown_to_segments(None) == []
    assert markdown_to_segments("   \n ") == []
    assert markdown_to_segments("<!-- only a comment -->") == []


def test_inline_formatting() -> None:
    out = html("**bold** *it* ~~gone~~ `code` [x](https://x.io)")
    assert out == (
        '<b>bold</b> <i>it</i> <s>gone</s> <code>code</code> <a href="https://x.io">x</a>'
    )


def test_escaping() -> None:
    assert html("a < b && c > d") == "a &lt; b &amp;&amp; c &gt; d"


def test_headings_are_bold_blocks() -> None:
    assert html("# Title\ntext") == "<b>Title</b>\n\ntext"


def test_soft_breaks_preserved() -> None:
    assert html("line one\nline two") == "line one\nline two"


def test_mentions_and_refs() -> None:
    out = html("thanks @octo-cat, fixes #12 (not email a@b.com or x#1)")
    assert '<a href="https://github.com/octo-cat">@octo-cat</a>' in out
    assert '<a href="https://github.com/o/r/issues/12">#12</a>' in out
    assert "a@b.com" in out
    assert "x#1" in out


def test_refs_without_repo_stay_plain() -> None:
    assert html("see #3", MdContext()) == "see #3"


def test_no_links_inside_links_or_code() -> None:
    out = html("[see @bob #1](https://x.io) `@bob`")
    assert out.count("<a ") == 1
    assert "<code>@bob</code>" in out


def test_code_not_inside_bold() -> None:
    assert html("**use `x`**") == "<b>use x</b>"


def test_lists_nested_and_tasks() -> None:
    out = html("- a\n  - b\n    - c\n- [x] done\n- [ ] todo\n\n3. three\n4. four")
    assert "• a" in out
    assert "   ◦ b" in out
    assert "      ▪ c" in out
    assert "• ✅ done" in out
    assert "• ⬜ todo" in out
    assert "3. three\n4. four" in out


def test_fenced_code_becomes_separate_segment() -> None:
    segs = markdown_to_segments("before\n\n```py\nprint('<x>')\n```\n\nafter", CTX)
    assert [s.kind for s in segs] == ["text", "code", "text"]
    assert segs[1].html == "<pre><code class=\"language-py\">print('&lt;x&gt;')</code></pre>"
    assert segs[1].lang == "py"


def test_long_code_is_clipped() -> None:
    body = "\n".join(f"line {i}" for i in range(100))
    seg = markdown_to_segments(f"```\n{body}\n```", MdContext(max_code_lines=5))[0]
    assert seg.raw.count("\n") == 5
    assert "95 more lines" in seg.raw


def test_blockquote_and_alert() -> None:
    out = html("> [!NOTE]\n> careful\n> here")
    assert out == "┃ <b>ℹ️ Note</b>\n┃ careful\n┃ here"
    assert "<blockquote" not in out  # body quotes are added by the formatter


def test_nested_quote_code_stays_inline() -> None:
    segs = markdown_to_segments("> ```\n> x\n> ```", CTX)
    assert [s.kind for s in segs] == ["text"]
    assert "┃ x" in segs[0].html


def test_narrow_table_is_monospace_grid() -> None:
    segs = markdown_to_segments("| A | B |\n|---|---|\n| 1 | two |", CTX)
    assert segs[0].kind == "code"
    assert segs[0].raw.splitlines() == ["A │ B", "──┼────", "1 │ two"]


def test_wide_table_degrades_to_lines() -> None:
    wide = "x" * 50
    out = html(f"| Name | Notes |\n|---|---|\n| foo | {wide} |")
    assert f"• <b>Name</b>: foo · <b>Notes</b>: {wide}" in out


def test_details_summary() -> None:
    out = html("<details>\n<summary>Logs</summary>\n\ninner **text**\n\n</details>")
    assert "<b>▸ Logs</b>" in out
    assert "inner <b>text</b>" in out
    assert "details" not in out


def test_template_noise_removed() -> None:
    md = "### Logs\n\n_No response_\n\n### Version\n\n1.2.3\n\n<!-- hidden -->"
    out = html(md)
    assert "Logs" not in out
    assert "No response" not in out
    assert out == "<b>Version</b>\n\n1.2.3"


def test_images_dropped_or_linked() -> None:
    md = "look ![shot](https://x.io/a.png) <img src='https://x.io/b.png'>"
    assert html(md) == "look"
    linked = html(md, MdContext(image_links=True))
    assert '🖼 <a href="https://x.io/a.png">shot</a>' in linked


def test_inline_html_supported_and_balanced() -> None:
    assert html("a<br>b <b>bold</b> <kbd>K</kbd> <sub>s</sub>") == "a\nb <b>bold</b> K s"
    # an unclosed tag never leaks out
    assert html("<b>open forever") == "<b>open forever</b>"


def test_html_block_converted() -> None:
    out = html("<div>\n<p>Hello <a href='https://x.io'>x</a></p>\n<ul><li>one</li></ul>\n</div>")
    assert "Hello" in out and '<a href="https://x.io">x</a>' in out and "• one" in out


def test_unsafe_links_never_linked() -> None:
    out = html("[a](javascript:alert(1)) <a href='javascript:x'>b</a>")
    assert 'href="javascript' not in out
    assert "<a" not in out


def test_relative_links_absolutized() -> None:
    assert (
        html("[f](/o/r/blob/main/a.py)") == '<a href="https://github.com/o/r/blob/main/a.py">f</a>'
    )


def test_emoji_shortcodes() -> None:
    assert html(":rocket: ship :unknown_code:") == "🚀 ship :unknown_code:"


def test_hr() -> None:
    assert "─────────" in html("a\n\n---\n\nb")


def test_output_is_always_sanitizer_stable() -> None:
    md = "**a _b ~~c `d`~~_** [<x>](https://e.io) > q\n\n> - [x] t\n\n|a|\n|-|\n|b|"
    for seg in markdown_to_segments(md, CTX):
        assert sanitize(seg.html) == seg.html


def test_crlf_normalised() -> None:
    assert clean_github_markdown("a\r\nb\r\n\r\n\r\n\r\nc") == "a\nb\n\nc"
