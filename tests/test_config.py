import pytest

from drive.config import GIB, ConfigError, load_settings

SETTING_NAMES = ("AUTH_MODE", "BIND_HOST", "PORT", "DATA_DIR", "TRUSTED_PROXIES", "USER_HEADER", "ALLOWED_USERS",
                 "LOCAL_USER", "SECRET_KEY", "SHARES_ENABLED", "MIN_FREE_GB", "MAX_FILE_GB", "ZIP_MAX_GB",
                 "PUBLIC_SCHEME", "PROXY_SECRET", "CONVERT_TIMEOUT_MINUTES", "CONVERT_MAX_INPUT_GB",
                 "CONVERT_MAX_OUTPUT_GB", "CONVERT_MEMORY_MB", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET",
                 "TOKEN_KEY_FILE", "SYNC_BIN_LIMIT_FILES", "SYNC_BIN_LIMIT_PERCENT")
GOOGLE_ID = "1234-abc.apps.googleusercontent.com"


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch, tmp_path):
    for name in SETTING_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)


def test_defaults_keep_share_links_off_and_fifteen_gb_free():
    settings = load_settings()
    assert settings.shares_enabled is False
    assert settings.min_free_bytes == 15 * GIB
    assert settings.bind_host == "127.0.0.1"


def test_share_links_and_the_disk_floor_are_settings(monkeypatch):
    monkeypatch.setenv("SHARES_ENABLED", "true")
    monkeypatch.setenv("MIN_FREE_GB", "30")
    settings = load_settings()
    assert settings.shares_enabled is True
    assert settings.min_free_bytes == 30 * GIB


def test_conversion_limits_are_settings(monkeypatch):
    assert load_settings().convert_seconds == 15 * 60
    monkeypatch.setenv("CONVERT_TIMEOUT_MINUTES", "5")
    monkeypatch.setenv("CONVERT_MAX_INPUT_GB", "2")
    monkeypatch.setenv("CONVERT_MEMORY_MB", "1024")
    settings = load_settings()
    assert (settings.convert_seconds, settings.convert_max_input_bytes) == (300, 2 * GIB)
    assert settings.convert_memory_bytes == 1024 * 1024 ** 2


def test_the_sync_bin_brake_is_a_setting(monkeypatch):
    settings = load_settings()
    assert (settings.sync_bin_limit_files, settings.sync_bin_limit_percent) == (100, 50)
    monkeypatch.setenv("SYNC_BIN_LIMIT_FILES", "500")
    monkeypatch.setenv("SYNC_BIN_LIMIT_PERCENT", "80")
    settings = load_settings()
    assert (settings.sync_bin_limit_files, settings.sync_bin_limit_percent) == (500, 80)


@pytest.mark.parametrize("name,value", [
    ("CONVERT_TIMEOUT_MINUTES", "0"), ("CONVERT_MEMORY_MB", "100"), ("CONVERT_MAX_OUTPUT_GB", "0"),
    ("SHARES_ENABLED", "maybe"), ("MIN_FREE_GB", "-1"), ("MIN_FREE_GB", "lots"), ("ZIP_MAX_GB", "0"),
    ("TRUSTED_PROXIES", "0.0.0.0/0"), ("TRUSTED_PROXIES", "10.1.2.16/24"), ("SECRET_KEY", "short"),
    ("AUTH_MODE", "none"), ("SYNC_BIN_LIMIT_FILES", "0"), ("SYNC_BIN_LIMIT_PERCENT", "101"),
])
def test_bad_settings_stop_the_start(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ConfigError):
        load_settings()


def test_local_mode_refuses_to_listen_beyond_this_machine(monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "local")
    monkeypatch.setenv("BIND_HOST", "0.0.0.0")
    with pytest.raises(ConfigError):
        load_settings()


def test_the_env_file_is_read(tmp_path):
    (tmp_path / ".env").write_text("# a note\nMIN_FREE_GB=30\nSHARES_ENABLED='false'\n")
    assert load_settings().min_free_bytes == 30 * GIB


def test_the_public_scheme_follows_the_sign_in_mode_and_can_be_set(monkeypatch):
    assert load_settings().public_scheme == "http"
    monkeypatch.setenv("AUTH_MODE", "proxy")
    monkeypatch.setenv("TRUSTED_PROXIES", "127.0.0.1")
    assert load_settings().public_scheme == "https"
    monkeypatch.setenv("AUTH_MODE", "local")
    assert load_settings().public_scheme == "http"
    monkeypatch.setenv("PUBLIC_SCHEME", "https")
    assert load_settings().public_scheme == "https"
    monkeypatch.setenv("PUBLIC_SCHEME", "ftp")
    with pytest.raises(ConfigError):
        load_settings()


def test_the_proxy_secret_must_be_long_and_only_for_proxy_mode(monkeypatch):
    assert load_settings().proxy_secret == ""
    monkeypatch.setenv("AUTH_MODE", "proxy")
    monkeypatch.setenv("TRUSTED_PROXIES", "127.0.0.1")
    monkeypatch.setenv("PROXY_SECRET", "short")
    with pytest.raises(ConfigError):
        load_settings()
    monkeypatch.setenv("PROXY_SECRET", "p" * 40)
    assert load_settings().proxy_secret == "p" * 40
    monkeypatch.setenv("AUTH_MODE", "local")
    with pytest.raises(ConfigError):
        load_settings()
    monkeypatch.setenv("AUTH_MODE", "builtin")
    with pytest.raises(ConfigError):
        load_settings()


def test_google_sign_in_is_off_until_both_values_are_set(monkeypatch, tmp_path):
    settings = load_settings()
    assert settings.google_ready is False
    assert settings.vault_key_path == settings.data_dir / "token.key"
    monkeypatch.setenv("GOOGLE_CLIENT_ID", GOOGLE_ID)
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "GOCSPX-stub")
    monkeypatch.setenv("TOKEN_KEY_FILE", str(tmp_path / "keys" / "token.key"))
    settings = load_settings()
    assert settings.google_ready is True
    assert settings.vault_key_path == tmp_path / "keys" / "token.key"


@pytest.mark.parametrize("client_id,client_secret", [
    (GOOGLE_ID, ""), ("", "GOCSPX-stub"), ("1234-abc.example.com", "GOCSPX-stub"), (GOOGLE_ID, "has space"),
])
def test_half_or_odd_google_settings_stop_the_start(monkeypatch, client_id, client_secret):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", client_id)
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", client_secret)
    with pytest.raises(ConfigError):
        load_settings()
