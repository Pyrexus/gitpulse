import pytest

from app.formatting.html import (
    esc,
    link,
    safe_url,
    sanitize,
    to_plain,
    truncate,
    utf16_len,
    visible_len,
)


def test_esc() -> None:
    assert esc("<b>&</b>") == "&lt;b&gt;&amp;&lt;/b&gt;"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://github.com/a", "https://github.com/a"),
        ("/owner/repo/pull/1", "https://github.com/owner/repo/pull/1"),
        ("//example.com/x", "https://example.com/x"),
        ("javascript:alert(1)", None),
        ("data:text/html,hi", None),
        ("#anchor", None),
        ("https://", None),
        ("", None),
        (None, None),
    ],
)
def test_safe_url(url: str | None, expected: str | None) -> None:
    assert safe_url(url) == expected


def test_link_escapes_quotes() -> None:
    out = link('https://x.io/?q="a"&b=1', "a<b")
    assert out == '<a href="https://x.io/?q=&quot;a&quot;&amp;b=1">a&lt;b</a>'
    assert link("javascript:x", "t") == "t"


def test_sanitize_balances_and_drops_unknown() -> None:
    assert sanitize("<b>bold <i>both</b> tail") == "<b>bold <i>both</i></b> tail"
    assert sanitize("<div>x</div></b>") == "x"
    assert sanitize("<b>open") == "<b>open</b>"


def test_sanitize_nesting_rules() -> None:
    # nothing inside code
    assert sanitize("<code><b>x</b></code>") == "<code>x</code>"
    # pre/code with language is preserved
    assert (
        sanitize('<pre><code class="language-python">x</code></pre>')
        == '<pre><code class="language-python">x</code></pre>'
    )
    # no nested blockquotes, no pre inside blockquote
    assert sanitize("<blockquote>a<blockquote>b</blockquote></blockquote>") == (
        "<blockquote>ab</blockquote>"
    )
    assert sanitize("<blockquote><pre>x</pre></blockquote>") == "<blockquote>x</blockquote>"
    # no links inside links
    assert sanitize('<a href="https://a.io"><a href="https://b.io">x</a></a>') == (
        '<a href="https://a.io">x</a>'
    )


def test_sanitize_attributes() -> None:
    assert sanitize('<a href="javascript:1">x</a>') == "x"
    assert sanitize('<a href="https://a.io" onclick="evil()">x</a>') == '<a href="https://a.io">x</a>'
    assert sanitize("<blockquote expandable>x</blockquote>") == "<blockquote expandable>x</blockquote>"
    assert sanitize('<span class="evil">x</span>') == "x"
    assert sanitize('<span class="tg-spoiler">x</span>') == '<span class="tg-spoiler">x</span>'


def test_sanitize_entities_and_br() -> None:
    assert sanitize("a &amp; b &lt; c &#128512; &nbsp;") == "a &amp; b &lt; c 😀 \xa0"
    assert sanitize("&bogus; x") == "&amp;bogus; x"
    assert sanitize("a<br>b<br/>c") == "a\nb\nc"
    assert sanitize("<script>alert(1)</script>ok") == "ok"


def test_truncate_keeps_markup_valid() -> None:
    src = "<b>Hello</b> <i>wonderful world of text</i>"
    out = truncate(src, 12)
    assert out.startswith("<b>Hello</b> <i>")
    assert out.endswith("</i>")
    assert visible_len(out) <= 12
    assert "…" in out


def test_truncate_never_cuts_entities() -> None:
    out = truncate("a &amp; b &amp; c &amp; d", 5)
    assert "&am" not in out.replace("&amp;", "")
    assert visible_len(out) <= 5


def test_truncate_noop_when_short() -> None:
    assert truncate("<b>hi</b>", 100) == "<b>hi</b>"


def test_utf16_len_counts_surrogates() -> None:
    assert utf16_len("a😀") == 3
    assert visible_len("<b>😀</b>") == 2
    assert truncate("😀😀😀😀", 5).count("😀") == 2


def test_to_plain() -> None:
    assert to_plain('<a href="https://x.io">x</a> &amp; <code>y</code>') == "x & y"
