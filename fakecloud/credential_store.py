#!/usr/bin/env python3

import ctypes
import getpass
import json
import os
from ctypes import wintypes

TARGET_NAME = "ConnectLife/local-controller"
CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2
ERROR_NOT_FOUND = 1168


class CredentialW(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPCWSTR),
        ("Comment", wintypes.LPCWSTR),
        ("LastWritten", wintypes.FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(wintypes.BYTE)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPCWSTR),
        ("UserName", wintypes.LPCWSTR),
    ]


def advapi32():
    if os.name != "nt":
        raise RuntimeError("Windows Credential Manager is required")
    return ctypes.windll.advapi32


def save_credentials(email, password):
    data = json.dumps({"email": email, "password": password}).encode("utf-8")
    buffer = ctypes.create_string_buffer(data)
    credential = CredentialW(
        Flags=0,
        Type=CRED_TYPE_GENERIC,
        TargetName=TARGET_NAME,
        Comment="ConnectLife local controller credentials",
        CredentialBlobSize=len(data),
        CredentialBlob=ctypes.cast(buffer, ctypes.POINTER(wintypes.BYTE)),
        Persist=CRED_PERSIST_LOCAL_MACHINE,
        AttributeCount=0,
        Attributes=None,
        TargetAlias=None,
        UserName=email,
    )
    if not advapi32().CredWriteW(ctypes.byref(credential), 0):
        raise RuntimeError(f"Credential Manager write failed: {ctypes.GetLastError()}")


def load_credentials():
    pointer = ctypes.POINTER(CredentialW)()
    if not advapi32().CredReadW(TARGET_NAME, CRED_TYPE_GENERIC, 0, ctypes.byref(pointer)):
        raise RuntimeError(f"Credential Manager read failed: {ctypes.GetLastError()}")
    try:
        credential = pointer.contents
        data = ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize)
        values = json.loads(data)
        return values["email"], values["password"]
    finally:
        advapi32().CredFree(pointer)


def setup_credentials():
    email = input("ConnectLife email: ").strip()
    password = getpass.getpass("ConnectLife password (not stored in plaintext): ")
    save_credentials(email, password)
    loaded_email, loaded_password = load_credentials()
    if loaded_email != email or loaded_password != password:
        raise RuntimeError("credential round-trip verification failed")
    print(f"Credentials saved in Windows Credential Manager as {TARGET_NAME}")


def reset_credentials():
    if not advapi32().CredDeleteW(TARGET_NAME, CRED_TYPE_GENERIC, 0):
        error = ctypes.GetLastError()
        if error != ERROR_NOT_FOUND:
            raise RuntimeError(f"Credential Manager delete failed: {error}")
    print("Stored credentials removed")
