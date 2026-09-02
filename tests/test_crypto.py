"""The per-tenant envelope encryption. Previously untested end to end: every
handler test stubbed encrypt_field to the identity function, so nothing
exercised a real Fernet round trip or the tenant boundary."""
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet, InvalidToken

from common import crypto, paramstore


@pytest.fixture(autouse=True)
def clear_cache():
    crypto._dek_cache.clear()
    yield
    crypto._dek_cache.clear()


def _with_keys(keys):
    """Patch Parameter Store so each tenant has its own DEK."""
    def read(tenant_id, kind):
        assert kind == paramstore.DEK, kind
        if tenant_id in keys:
            return keys[tenant_id]
        raise AssertionError(f"unexpected tenant {tenant_id}")

    return patch.object(crypto.paramstore, "read", side_effect=read)


def test_a_field_round_trips_through_the_tenants_own_key():
    with _with_keys({"acme": crypto.generate_dek()}):
        ciphertext = crypto.encrypt_field("acme", "the pool ran out")
        assert ciphertext != "the pool ran out"
        assert crypto.decrypt_field("acme", ciphertext) == "the pool ran out"


def test_one_tenant_cannot_decrypt_another_tenants_field():
    """The property the whole design rests on."""
    keys = {"acme": crypto.generate_dek(), "globex": crypto.generate_dek()}
    with _with_keys(keys):
        ciphertext = crypto.encrypt_field("acme", "acme's incident detail")
        with pytest.raises(InvalidToken):
            crypto.decrypt_field("globex", ciphertext)


def test_a_rotated_key_cannot_read_the_old_ciphertext():
    """Documents the consequence: query_diagnostics has to survive this rather
    than 500, which is why it catches InvalidToken."""
    with _with_keys({"acme": crypto.generate_dek()}):
        ciphertext = crypto.encrypt_field("acme", "secret")
    crypto._dek_cache.clear()
    with _with_keys({"acme": crypto.generate_dek()}):
        with pytest.raises(InvalidToken):
            crypto.decrypt_field("acme", ciphertext)


def test_none_passes_through_unchanged():
    with _with_keys({"acme": crypto.generate_dek()}):
        assert crypto.encrypt_field("acme", None) is None
        assert crypto.decrypt_field("acme", None) is None


def test_the_dek_is_fetched_once_and_cached():
    with _with_keys({"acme": crypto.generate_dek()}) as mock:
        crypto.encrypt_field("acme", "a")
        crypto.encrypt_field("acme", "b")
        crypto.encrypt_field("acme", "c")
    assert mock.call_count == 1


def test_generated_deks_are_valid_fernet_keys_and_unique():
    a, b = crypto.generate_dek(), crypto.generate_dek()
    assert a != b
    Fernet(a.encode("utf-8"))
    Fernet(b.encode("utf-8"))


def test_unicode_survives_the_round_trip():
    with _with_keys({"acme": crypto.generate_dek()}):
        text = "réplica lag 5s — ‹redacted:email›"
        assert crypto.decrypt_field("acme", crypto.encrypt_field("acme", text)) == text
