import json
import os
from cryptography.fernet import Fernet


def _cipher():
    key = os.environ.get("INSTRADAR_FERNET_KEY", "")
    if not key:
        if os.environ.get("INSTRADAR_TEST") == "1":
            key = "dPuJ3zYpVzrfHiPOFU-QDpQQ0MfKnDvjeZeY3WGy50M="
        else:
            raise RuntimeError("INSTRADAR_FERNET_KEY is required")
    return Fernet(key.encode("ascii"))


def seal(data):
    return _cipher().encrypt(json.dumps(data, ensure_ascii=False).encode("utf-8"))


def unseal(data):
    return json.loads(_cipher().decrypt(bytes(data)).decode("utf-8"))
