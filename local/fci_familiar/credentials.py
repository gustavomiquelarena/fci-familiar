"""Fernet credentials and Windows Credential Manager, without import-time IO.

The legacy ``encrypted_config.ini`` is an authenticated Fernet blob containing
a JSON object. The master key remains in the current Windows user's vault.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


class CredentialError(RuntimeError):
    """An actionable error whose message never contains secret material."""


class _CREDENTIALW(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD), ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p), ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


def _windows_api():
    if os.name != "nt":
        raise CredentialError("El almacén de credenciales requiere Windows")
    api = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    api.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                             ctypes.POINTER(ctypes.POINTER(_CREDENTIALW))]
    api.CredReadW.restype = wintypes.BOOL
    api.CredWriteW.argtypes = [ctypes.POINTER(_CREDENTIALW), wintypes.DWORD]
    api.CredWriteW.restype = wintypes.BOOL
    api.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    api.CredDeleteW.restype = wintypes.BOOL
    api.CredFree.argtypes = [ctypes.c_void_p]
    api.CredFree.restype = None
    return api


def _target(value):
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise CredentialError("Indicar el nombre de la credencial de Windows")
    return value


class SecretStore:
    """Only accesses the explicitly named generic credential, never enumerates.

    Uses pywin32 when installed; the native Windows API fallback allows the
    isolated application to run on a Python version without a pywin32 wheel.
    """

    @staticmethod
    def get_bytes(target: str) -> bytes:
        target = _target(target)
        if os.name != "nt":
            raise CredentialError("El almacén de credenciales requiere Windows")
        try:
            import win32cred
        except ImportError:
            win32cred = None
        try:
            if win32cred is not None:
                blob = win32cred.CredRead(target, win32cred.CRED_TYPE_GENERIC, 0)["CredentialBlob"]
                return blob.encode("utf-8") if isinstance(blob, str) else bytes(blob)
            api = _windows_api()
            pointer = ctypes.POINTER(_CREDENTIALW)()
            if not api.CredReadW(target, 1, 0, ctypes.byref(pointer)):
                raise OSError(ctypes.get_last_error())
            try:
                return ctypes.string_at(pointer.contents.CredentialBlob,
                                        pointer.contents.CredentialBlobSize)
            finally:
                api.CredFree(pointer)
        except Exception:
            raise CredentialError("No se pudo leer la credencial de Windows solicitada") from None

    @staticmethod
    def get(target: str) -> str:
        blob = SecretStore.get_bytes(target)
        try:
            # Older Windows tools sometimes saved Unicode strings as UTF-16.
            if b"\x00" in blob:
                return blob.decode("utf-16-le")
            return blob.decode("utf-8")
        except UnicodeError:
            raise CredentialError("La credencial de Windows no contiene texto válido") from None

    @staticmethod
    def set(target: str, value: str) -> None:
        target = _target(target)
        if not isinstance(value, str) or not value or "\x00" in value:
            raise CredentialError("La contraseña no puede estar vacía")
        data = value.encode("utf-8")
        if len(data) > 2560:
            raise CredentialError("La contraseña excede el tamaño del almacén de Windows")
        if os.name != "nt":
            raise CredentialError("El almacén de credenciales requiere Windows")
        try:
            import win32cred
        except ImportError:
            win32cred = None
        try:
            if win32cred is not None:
                win32cred.CredWrite({"Type": win32cred.CRED_TYPE_GENERIC,
                    "TargetName": target, "CredentialBlob": data,
                    "Persist": win32cred.CRED_PERSIST_LOCAL_MACHINE,
                    "UserName": "FCIFamiliar"}, 0)
                return
            api = _windows_api()
            buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
            credential = _CREDENTIALW()
            credential.Type = 1
            credential.TargetName = target
            credential.CredentialBlobSize = len(data)
            credential.CredentialBlob = buffer
            credential.Persist = 2  # Local-machine persistence for this user.
            credential.UserName = "FCIFamiliar"
            if not api.CredWriteW(ctypes.byref(credential), 0):
                raise OSError(ctypes.get_last_error())
        except Exception:
            raise CredentialError("No se pudo guardar la credencial en Windows") from None

    @staticmethod
    def delete(target: str) -> None:
        target = _target(target)
        if os.name != "nt":
            raise CredentialError("El almacén de credenciales requiere Windows")
        try:
            import win32cred
        except ImportError:
            win32cred = None
        try:
            if win32cred is not None:
                try:
                    win32cred.CredDelete(target, win32cred.CRED_TYPE_GENERIC, 0)
                except Exception as exc:
                    if getattr(exc, "winerror", None) != 1168:
                        raise
                return
            api = _windows_api()
            if not api.CredDeleteW(target, 1, 0) and ctypes.get_last_error() != 1168:
                raise OSError(ctypes.get_last_error())
        except Exception:
            raise CredentialError("No se pudo eliminar la credencial de Windows") from None


def load_key_from_windows_credentials(target="secret_key") -> bytes:
    raw = SecretStore.get_bytes(target)
    candidates = [raw]
    if b"\x00" in raw:
        try:
            candidates.append(raw.decode("utf-16-le").encode("ascii"))
        except (UnicodeError, ValueError):
            pass
    for candidate in candidates:
        try:
            Fernet(candidate)
            return candidate
        except (ValueError, TypeError):
            pass
    raise CredentialError("La credencial seleccionada no contiene una clave Fernet válida")


def load_credentials(key_name, config_path=None, target="secret_key", *, credential_target=None):
    """Read the authorized encrypted file; preserve the legacy JSON format."""
    if not isinstance(key_name, str) or not key_name:
        raise CredentialError("Indicar el nombre de la credencial")
    raw_path = config_path or os.environ.get("ENCRYPTED_CONFIG_PATH")
    if not raw_path:
        raise CredentialError("Configurar la ruta del archivo de credenciales cifradas")
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        raise CredentialError("La ruta del archivo cifrado debe ser absoluta")
    key = load_key_from_windows_credentials(credential_target or target)
    try:
        with path.open("rb") as stream:
            encrypted = stream.read(1024 * 1024 + 1)
        if not encrypted or len(encrypted) > 1024 * 1024:
            raise ValueError("Invalid size")
        values = json.loads(Fernet(key).decrypt(encrypted).decode("utf-8"))
    except (OSError, ValueError, TypeError, UnicodeError, InvalidToken):
        raise CredentialError("No se pudo leer un archivo de credenciales válido") from None
    if not isinstance(values, dict):
        raise CredentialError("El archivo descifrado debe contener un objeto JSON")
    value = values.get(key_name)
    if not isinstance(value, str) or not value.strip():
        raise CredentialError("La credencial solicitada falta o no contiene texto válido")
    return value
