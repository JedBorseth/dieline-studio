"""Test-wide setup."""

import os

# Tests never touch the real credential store. On a headless machine with no keyring
# service, asking the system keyring blocks, so the null backend is the default here.
os.environ.setdefault("PYTHON_KEYRING_BACKEND", "keyring.backends.null.Keyring")
