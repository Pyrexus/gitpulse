import pytest

from app.security.crypto import CryptoError, SecretBox, generate_key


@pytest.fixture
def box() -> SecretBox:
    return SecretBox(generate_key())


def test_roundtrip(box: SecretBox) -> None:
    ct = box.encrypt("ghp_secret_value", "pat:42")
    assert ct.startswith("v1.")
    assert "ghp_secret_value" not in ct
    assert box.decrypt(ct, "pat:42") == "ghp_secret_value"


def test_nonce_is_random(box: SecretBox) -> None:
    assert box.encrypt("x", "c") != box.encrypt("x", "c")


def test_context_binding(box: SecretBox) -> None:
    ct = box.encrypt("token", "pat:1")
    with pytest.raises(CryptoError):
        box.decrypt(ct, "pat:2")


def test_wrong_key_fails(box: SecretBox) -> None:
    ct = box.encrypt("token", "pat:1")
    with pytest.raises(CryptoError):
        SecretBox(generate_key()).decrypt(ct, "pat:1")


def test_tampering_detected(box: SecretBox) -> None:
    ct = box.encrypt("token", "ctx")
    tampered = ct[:-4] + ("AAAA" if not ct.endswith("AAAA") else "BBBB")
    with pytest.raises(CryptoError):
        box.decrypt(tampered, "ctx")


@pytest.mark.parametrize("bad", ["", "abc", "v2.AAAA", "v1.!!!"])
def test_malformed(box: SecretBox, bad: str) -> None:
    with pytest.raises(CryptoError):
        box.decrypt(bad, "ctx")


def test_invalid_key_rejected() -> None:
    with pytest.raises(CryptoError):
        SecretBox("dG9vLXNob3J0")


def test_subkeys_are_distinct(box: SecretBox) -> None:
    assert box.subkey("callbacks") != box.subkey("other")
    assert len(box.subkey("callbacks")) == 32
