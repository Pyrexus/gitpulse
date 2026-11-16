import pytest

from app.security.signing import (
    CallbackError,
    CallbackExpired,
    CallbackSigner,
    sign_github_payload,
    verify_github_signature,
)

BODY = b'{"zen":"Keep it logically awesome."}'


def test_github_signature_valid() -> None:
    sig = sign_github_payload("s3cret", BODY)
    assert verify_github_signature("s3cret", BODY, sig)


@pytest.mark.parametrize(
    "header",
    [None, "", "sha1=abc", "sha256=deadbeef", sign_github_payload("other", BODY)],
)
def test_github_signature_invalid(header: str | None) -> None:
    assert not verify_github_signature("s3cret", BODY, header)


def test_github_signature_body_tamper() -> None:
    sig = sign_github_payload("s3cret", BODY)
    assert not verify_github_signature("s3cret", BODY + b" ", sig)


def test_github_signature_empty_secret() -> None:
    assert not verify_github_signature("", BODY, sign_github_payload("", BODY))


@pytest.fixture
def signer() -> CallbackSigner:
    return CallbackSigner(b"k" * 32)


def test_callback_roundtrip(signer: CallbackSigner) -> None:
    data = signer.pack("it", 123, "pr", now=1_800_000_000)
    assert len(data.encode()) <= 64
    cb = signer.unpack(data, ttl_hours=48, now=1_800_000_000 + 3600)
    assert cb.action == "it"
    assert cb.args == ("123", "pr")
    assert cb.int_arg(0) == 123
    assert cb.arg(5, "x") == "x"


def test_callback_tampered(signer: CallbackSigner) -> None:
    data = signer.pack("rm", 1)
    with pytest.raises(CallbackError):
        signer.unpack(data.replace("rm:1", "rm:2"), ttl_hours=None)


def test_callback_other_key(signer: CallbackSigner) -> None:
    data = signer.pack("home")
    with pytest.raises(CallbackError):
        CallbackSigner(b"x" * 32).unpack(data, ttl_hours=None)


def test_callback_expiry(signer: CallbackSigner) -> None:
    data = signer.pack("home", now=0)
    with pytest.raises(CallbackExpired):
        signer.unpack(data, ttl_hours=48, now=49 * 3600)
    # Persistent actions ignore age.
    assert signer.unpack(data, ttl_hours=None, now=10_000 * 3600).action == "home"


def test_callback_rejects_separators(signer: CallbackSigner) -> None:
    with pytest.raises(ValueError):
        signer.pack("x", "a:b")


def test_callback_too_long(signer: CallbackSigner) -> None:
    with pytest.raises(ValueError):
        signer.pack("x", "y" * 60)


def test_peek_action() -> None:
    assert CallbackSigner.peek_action("st:7d:abc.sig") == "st"
    assert CallbackSigner.peek_action("garbage") == "garbage"
