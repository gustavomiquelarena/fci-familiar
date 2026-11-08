"""Formato interoperable AES-GCM / Web Crypto. Nunca publica contraseñas."""
from __future__ import annotations

import base64
import json
import os
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

ITERATIONS = 600_000


def _key(password: str, salt: bytes, iterations: int = ITERATIONS) -> bytes:
    if not isinstance(password, str) or not password:
        raise ValueError("Ingresá una contraseña")
    return PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=iterations).derive(password.encode("utf-8"))


def encrypt_report(report: dict, password: str) -> dict:
    salt, iv = os.urandom(16), os.urandom(12)
    plaintext = json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    ciphertext = AESGCM(_key(password, salt)).encrypt(iv, plaintext, None)
    enc = lambda value: base64.b64encode(value).decode("ascii")
    return {"version": 1, "kdf": "PBKDF2-SHA256", "iterations": ITERATIONS, "salt": enc(salt), "iv": enc(iv), "ciphertext": enc(ciphertext)}


def decrypt_report(envelope: dict, password: str) -> dict:
    if envelope.get("version") != 1 or envelope.get("kdf") != "PBKDF2-SHA256" or envelope.get("iterations") != ITERATIONS:
        raise ValueError("Formato de reporte no compatible")
    dec = lambda field: base64.b64decode(envelope[field], validate=True)
    salt, iv, ciphertext = dec("salt"), dec("iv"), dec("ciphertext")
    if len(salt) != 16 or len(iv) != 12 or len(ciphertext) < 16:
        raise ValueError("Reporte inválido")
    return json.loads(AESGCM(_key(password, salt)).decrypt(iv, ciphertext, None))
