import pytest

from drive.vault import SecretUnreadable, Vault, VaultKeyError, load_or_create_vault_key


def test_the_key_is_made_once_and_kept_private(tmp_path):
    key_path = tmp_path / "data" / "token.key"
    first = load_or_create_vault_key(key_path)
    assert key_path.stat().st_mode & 0o777 == 0o600
    assert load_or_create_vault_key(key_path) == first


def test_a_loosened_key_file_is_tightened_again(tmp_path):
    key_path = tmp_path / "token.key"
    load_or_create_vault_key(key_path)
    key_path.chmod(0o644)
    load_or_create_vault_key(key_path)
    assert key_path.stat().st_mode & 0o777 == 0o600


def test_sealed_secrets_open_only_with_the_same_key(tmp_path):
    vault = Vault(load_or_create_vault_key(tmp_path / "one.key"))
    sealed = vault.seal("app-password-123")
    assert b"app-password-123" not in sealed
    assert vault.open(sealed) == "app-password-123"
    other = Vault(load_or_create_vault_key(tmp_path / "two.key"))
    with pytest.raises(SecretUnreadable):
        other.open(sealed)
    with pytest.raises(SecretUnreadable):
        vault.open(None)


def test_a_damaged_key_file_stops_the_start(tmp_path):
    key_path = tmp_path / "token.key"
    key_path.write_text("not a key")
    with pytest.raises(VaultKeyError):
        load_or_create_vault_key(key_path)
