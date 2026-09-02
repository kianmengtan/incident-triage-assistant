"""Application-layer envelope encryption using a per-tenant DEK.

Each tenant has a symmetric data-encryption key stored as a Fernet key in SSM
Parameter Store (``/{PREFIX}/tenant/{tenant_id}/dek`` -- see
:mod:`common.paramstore` for why it is not in Secrets Manager). Sensitive fields
are encrypted with it before being written to DynamoDB/S3 so that pooled storage
never holds another tenant's plaintext even if a row were misrouted.
"""
import time

from cryptography.fernet import Fernet

from . import paramstore

_dek_cache = {}
_DEK_CACHE_TTL_SECONDS = 5 * 60


def get_fernet(tenant_id):
    cached = _dek_cache.get(tenant_id)
    now = time.time()
    if cached and now - cached["fetched_at"] < _DEK_CACHE_TTL_SECONDS:
        return cached["fernet"]

    key = paramstore.read(tenant_id, paramstore.DEK).encode("utf-8")
    fernet = Fernet(key)
    _dek_cache[tenant_id] = {"fernet": fernet, "fetched_at": now}
    return fernet


def encrypt_field(tenant_id, plaintext):
    if plaintext is None:
        return None
    fernet = get_fernet(tenant_id)
    return fernet.encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_field(tenant_id, ciphertext):
    if ciphertext is None:
        return None
    fernet = get_fernet(tenant_id)
    return fernet.decrypt(ciphertext.encode("utf-8")).decode("utf-8")


def generate_dek():
    return Fernet.generate_key().decode("utf-8")
