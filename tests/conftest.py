import keyring
import pytest
from keyring.backends.fail import Keyring as NoKeyring


@pytest.fixture(autouse=True)
def isolated_token_store(monkeypatch, tmp_path):
    """Keep tests away from the developer's keyring and stored tokens."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    for name in ("YANDEX_OAUTH_TOKEN", "YANDEX_OAUTH_CLIENT_ID", "YANDEX_OAUTH_CLIENT_SECRET"):
        monkeypatch.delenv(name, raising=False)
    previous = keyring.get_keyring()
    keyring.set_keyring(NoKeyring())
    yield
    keyring.set_keyring(previous)
