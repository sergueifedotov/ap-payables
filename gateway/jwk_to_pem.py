#!/usr/bin/env python3
"""Turn the SPIRE JWT-SVID public keys into PEM for Vault's jwt auth method."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import jwt
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat


def main() -> None:
    doc = json.loads(Path(sys.argv[1]).read_text())
    wrote = 0
    for entry in doc.get("keys", []):
        if entry.get("use") != "jwt-svid":
            continue
        key = jwt.PyJWK(entry).key
        sys.stdout.buffer.write(key.public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo))
        wrote += 1
    if wrote == 0:
        raise SystemExit("no jwt-svid keys in the SPIRE bundle")


if __name__ == "__main__":
    main()
