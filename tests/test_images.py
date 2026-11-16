from app.formatting.images import (
    extract_html_images,
    extract_images,
    is_user_attachment,
    looks_like_image_url,
)

UA = "https://github.com/user-attachments/assets/6f1c2c5e-1111-2222-3333-444455556666"


def test_markdown_and_img_tags_in_order() -> None:
    md = (
        "first ![a](https://x.io/1.png)\n"
        f'<img width="300" alt="shot" src="{UA}" />\n'
        '![b](<https://x.io/2.jpg> "title")\n'
        "<IMG SRC='https://x.io/3.webp'>"
    )
    assert extract_images(md) == [
        "https://x.io/1.png",
        UA,
        "https://x.io/2.jpg",
        "https://x.io/3.webp",
    ]


def test_reference_style_images() -> None:
    md = "![logo][l]\n\n[l]: https://x.io/logo.png"
    assert extract_images(md) == ["https://x.io/logo.png"]


def test_dedupe_and_limit() -> None:
    md = "".join(f"![](https://x.io/{i % 3}.png)" for i in range(20))
    assert len(extract_images(md)) == 3
    md = "".join(f"![](https://x.io/{i}.png)" for i in range(20))
    assert len(extract_images(md)) == 10


def test_badges_svg_and_emoji_skipped() -> None:
    md = (
        "![ci](https://github.com/o/r/actions/workflows/ci.yml/badge.svg)"
        "![v](https://img.shields.io/npm/v/x)"
        "![e](https://github.githubassets.com/images/icons/emoji/unicode/1f44d.png)"
        "![s](https://x.io/diagram.svg)"
        "![ok](https://x.io/ok.png)"
    )
    assert extract_images(md) == ["https://x.io/ok.png"]


def test_images_in_code_ignored() -> None:
    md = "```\n![x](https://x.io/no.png)\n```\n`![y](https://x.io/no2.png)` ![z](https://x.io/yes.png)"
    assert extract_images(md) == ["https://x.io/yes.png"]


def test_unsafe_and_relative() -> None:
    assert extract_images("![x](javascript:alert(1)) ![y](data:image/png;base64,AAA)") == []
    assert extract_images("![r](/o/r/raw/main/a.png)") == ["https://github.com/o/r/raw/main/a.png"]


def test_dead_or_garbage_markup_does_not_crash() -> None:
    assert extract_images("![broken](") == []
    assert extract_images("<img src=>") == []
    assert extract_images(None) == []


def test_body_html_signed_urls() -> None:
    html = (
        '<p><a href="https://private-user-images.githubusercontent.com/1/abc.png?jwt=eyJ&amp;x=1">'
        '<img src="https://private-user-images.githubusercontent.com/1/abc.png?jwt=eyJ&amp;x=1" '
        'alt="shot"></a></p>'
    )
    assert extract_html_images(html) == [
        "https://private-user-images.githubusercontent.com/1/abc.png?jwt=eyJ&x=1"
    ]


def test_url_classifiers() -> None:
    assert is_user_attachment(UA)
    assert is_user_attachment("https://private-user-images.githubusercontent.com/1/a.png")
    assert not is_user_attachment("https://x.io/a.png")
    assert looks_like_image_url("https://x.io/a.JPG")
    assert not looks_like_image_url("https://x.io/readme")
