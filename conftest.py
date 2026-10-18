"""Repo-wide pytest setup (loaded for every test path).

Tests never read a developer's OS keychain or ``~/.config/picky``: the ``utils.secrets`` vault is switched off for the
whole run, so ``Settings()`` resolves env + repo ``.env`` + defaults only. ``utils/tests/test_secrets.py`` turns it
back on per test with an in-memory keyring and a temporary HOME.
"""

import os

os.environ["PICKY_SECRETS_BACKEND"] = "off"
