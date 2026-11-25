import os
import re
import subprocess
import tarfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
CORE_ADDRESS = "10.0.0.10"
PROXY_ADDRESS = "10.0.0.16"
SITE_VALUES = {
    "PROXMOX": "proxmox-host", "CORE_CT": "4100", "PROXY_CT": "4109", "DRIVE_DATA": "/srv/drive-data",
    "CORE_ADDRESS": CORE_ADDRESS, "HOST_PORT": "5006", "PROXY_ADDRESS": PROXY_ADDRESS, "SITE_HOST": "drive.example.com",
    "SITE_DOMAIN": "example.com", "OTHER_SITE_NAMES": "keep tasks", "SIGNED_IN_USER": "admin",
}
DEPLOY_FILES = ("compose.yaml", "drive.env", "ufw-after.rules.drive", "Caddyfile.drive")
RUNTIME_PLACEHOLDERS = ("__UPSTREAM__", "__PROXY_SECRET__")
AFTER_RULES = f"""*filter
:ufw-after-input - [0:0]
COMMIT

*filter
:DOCKER-USER - [0:0]
-A DOCKER-USER -p tcp -m conntrack --ctorigdstport 5003 --ctdir ORIGINAL ! -s {PROXY_ADDRESS} -j DROP
-A DOCKER-USER -j RETURN
COMMIT
"""
CADDYFILE = "{\n\tadmin off\n}\n\nhttp://keep.example.com {\n\treverse_proxy 127.0.0.1:8097\n}\n"
LIVE_RULE = f"-A DOCKER-USER ! -s {PROXY_ADDRESS}/32 -p tcp -m conntrack --ctorigdstport 5006 --ctdir ORIGINAL -j DROP"
UFW_STUB = """#!/bin/sh
echo "ufw $*" >> "$STUB_LOG"
case "$1" in
  status) echo "Status: ${STUB_UFW:-active}" ;;
  reload) [ "${STUB_UFW_RELOAD:-ok}" = ok ] || { echo "ERROR: problem running ufw-init" >&2; exit 1; } ;;
esac
"""
IPTABLES_STUB = f"""#!/bin/sh
echo "iptables $*" >> "$STUB_LOG"
echo "-N DOCKER-USER"
if [ "${{STUB_LIVE_RULE:-yes}}" = yes ]; then echo "{LIVE_RULE}"; fi
echo "-A DOCKER-USER -j RETURN"
"""
DOCKER_STUB = """#!/bin/sh
echo "docker $*" >> "$STUB_LOG"
case "$1" in
  exec) echo '{"ok":true}' ;;
  inspect) echo running ;;
esac
"""


PROXY_SECRET = "ab" * 32
APP_HEADROOM_MB = 512


def secret_file(tmp_path: Path) -> Path:
    path = tmp_path / "drive-proxy-secret"
    path.write_text(PROXY_SECRET + "\n")
    return path


def site_file(folder: Path, values: dict = SITE_VALUES) -> Path:
    path = folder / "site.env"
    path.write_text("".join(f"{name}={value}\n" for name, value in values.items()))
    return path


def deploy_sh(tmp_path: Path, *args: str, site: Path | None = None, env: dict | None = None) -> subprocess.CompletedProcess:
    environment = {name: value for name, value in os.environ.items() if name not in SITE_VALUES}
    environment.update(env or {}, SITE_ENV=str(site or site_file(tmp_path)))
    return subprocess.run(["bash", str(REPO / "deploy.sh"), *args], capture_output=True, text=True, env=environment,
                          timeout=60)


def rendered(tmp_path: Path, name: str) -> Path:
    answer = deploy_sh(tmp_path, "print-deploy-file", name)
    assert answer.returncode == 0, answer.stdout + answer.stderr
    path = tmp_path / f"rendered-{name}"
    path.write_text(answer.stdout)
    return path


def pushed_block(tmp_path: Path) -> Path:
    path = tmp_path / "drive-Caddyfile.block"
    path.write_text(rendered(tmp_path, "Caddyfile.drive").read_text())
    return path


def side_script(tmp_path: Path, which: str) -> Path:
    answer = deploy_sh(tmp_path, f"print-{which}-script")
    assert answer.returncode == 0, answer.stdout + answer.stderr
    text = answer.stdout
    script = tmp_path / f"{which}.sh"
    script.write_text(text)
    return script


@pytest.fixture
def stubs(tmp_path):
    folder = tmp_path / "stubs"
    folder.mkdir()
    log = tmp_path / "stub.log"
    for name, body in {
        "caddy": '#!/bin/sh\necho "caddy $*" >> "$STUB_LOG"\nif grep -q BROKEN "$3"; then exit 1; fi\nexit 0\n',
        "systemctl": '#!/bin/sh\necho "systemctl $*" >> "$STUB_LOG"\n',
        "ufw": UFW_STUB,
        "iptables": IPTABLES_STUB,
        "docker": DOCKER_STUB,
    }.items():
        stub = folder / name
        stub.write_text(body)
        stub.chmod(0o755)
    return {"PATH": f"{folder}:{os.environ['PATH']}", "STUB_LOG": str(log), "log": log}


def run(script: Path, env: dict, *args, stdin: str | None = None) -> subprocess.CompletedProcess:
    clean = {key: value for key, value in env.items() if key != "log"}
    return subprocess.run(["bash", str(script), *args], env={**os.environ, **clean}, capture_output=True, text=True,
                          input=stdin)


def test_every_script_parses():
    for path in (REPO / "deploy.sh",):
        subprocess.run(["bash", "-n", str(path)], check=True)


def test_the_ufw_rule_goes_before_the_return_once_and_comes_out_cleanly(tmp_path, stubs):
    script = side_script(tmp_path, "core")
    rules = tmp_path / "after.rules"
    rules.write_text(AFTER_RULES)
    env = {**stubs, "AFTER_RULES": str(rules), "UFW_LOCK": str(tmp_path / "lock"), "STATE": str(tmp_path)}
    rule_file = rendered(tmp_path, "ufw-after.rules.drive")
    assert run(script, env, "add-rule", str(rule_file)).returncode == 0
    lines = rules.read_text().splitlines()
    drop = rule_file.read_text().strip()
    assert lines.index(drop) < lines.index("-A DOCKER-USER -j RETURN")
    assert "--ctorigdstport 5003" in rules.read_text()
    assert run(script, env, "add-rule", str(rule_file)).returncode == 0
    assert rules.read_text().count(drop) == 1
    assert "ufw reload" in stubs["log"].read_text()
    assert not (tmp_path / "lock").exists()
    assert run(script, env, "remove-rule").returncode == 0
    assert rules.read_text() == AFTER_RULES


def test_the_ufw_rule_is_refused_without_a_return_line(tmp_path, stubs):
    script = side_script(tmp_path, "core")
    rules = tmp_path / "after.rules"
    rules.write_text("*filter\nCOMMIT\n")
    env = {**stubs, "AFTER_RULES": str(rules), "UFW_LOCK": str(tmp_path / "lock"), "STATE": str(tmp_path)}
    answer = run(script, env, "add-rule", str(rendered(tmp_path, "ufw-after.rules.drive")))
    assert answer.returncode != 0
    assert rules.read_text() == "*filter\nCOMMIT\n"
    assert not (tmp_path / "lock").exists()


def test_the_caddy_block_is_added_once_filled_in_and_removed_cleanly(tmp_path, stubs):
    script = side_script(tmp_path, "proxy")
    caddyfile = tmp_path / "Caddyfile"
    caddyfile.write_text(CADDYFILE)
    env = {**stubs, "CADDYFILE": str(caddyfile), "CADDY_LOCK": str(tmp_path / "lock"), "STATE": str(tmp_path),
           "SECRET_FILE": str(secret_file(tmp_path))}
    block = pushed_block(tmp_path)
    assert run(script, env, "add-site", str(block)).returncode == 0
    assert not block.exists()
    text = caddyfile.read_text()
    assert text.startswith(CADDYFILE)
    assert "__UPSTREAM__" not in text and f"reverse_proxy {CORE_ADDRESS}:5006" in text
    assert "systemctl reload caddy" in stubs["log"].read_text()
    assert run(script, env, "add-site", str(pushed_block(tmp_path))).returncode == 0
    assert caddyfile.read_text().count("http://drive.example.com {") == 1
    assert run(script, env, "remove-site").returncode == 0
    assert caddyfile.read_text() == CADDYFILE
    assert not (tmp_path / "lock").exists()


def test_a_caddyfile_that_does_not_validate_is_put_back(tmp_path, stubs):
    script = side_script(tmp_path, "proxy")
    caddyfile = tmp_path / "Caddyfile"
    caddyfile.write_text(CADDYFILE)
    broken = tmp_path / "broken.block"
    broken.write_text("http://drive.example.com {\n\tBROKEN\n}\n")
    env = {**stubs, "CADDYFILE": str(caddyfile), "CADDY_LOCK": str(tmp_path / "lock"), "STATE": str(tmp_path),
           "SECRET_FILE": str(secret_file(tmp_path))}
    assert run(script, env, "add-site", str(broken)).returncode != 0
    assert caddyfile.read_text() == CADDYFILE
    assert "systemctl reload caddy" not in stubs["log"].read_text()


def test_the_share_path_skips_the_login_and_nothing_else_does():
    block = (REPO / "deploy" / "Caddyfile.drive").read_text()
    share_handle = block[block.index("@share_link path /s/*"):block.index("@signed_out")]
    assert "forward_auth" not in share_handle
    assert "X-Forwarded-User" not in share_handle
    assert block.count("forward_auth") == 1
    assert "request_header -X-Forwarded-User" in block
    assert block.index("request_header -X-Forwarded-User") < block.index("@share_link")
    signed_out = block[block.index("@signed_out {"):block.index("redir @signed_out")]
    assert "not path /signin/* /api/auth/login /s/*" in signed_out


def test_the_server_settings_ship_with_share_links_off(tmp_path):
    env = rendered(tmp_path, "drive.env").read_text().splitlines()
    assert "SHARES_ENABLED=false" in env
    assert f"TRUSTED_PROXIES={PROXY_ADDRESS}" in env
    assert "ALLOWED_USERS=admin" in env
    assert "MIN_FREE_GB=15" in env


@pytest.fixture
def core(tmp_path, stubs):
    script = side_script(tmp_path, "core")
    rules = tmp_path / "after.rules"
    rules.write_text(AFTER_RULES)
    app = tmp_path / "app"
    (app / "deploy").mkdir(parents=True)
    (app / "deploy" / "compose.yaml").write_text("services: {}\n")
    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    conf = tmp_path / "conf"
    conf.mkdir()
    env = {**stubs, "AFTER_RULES": str(rules), "UFW_LOCK": str(tmp_path / "lock"), "STATE": str(tmp_path),
           "APP": str(app), "DATA": str(data), "CONF": str(conf), "CONTAINER_USER": str(os.getuid())}
    assert run(script, env, "add-rule", str(rendered(tmp_path, "ufw-after.rules.drive"))).returncode == 0
    stubs["log"].write_text("")
    return {"script": script, "env": env, "rules": rules, "app": app, "log": stubs["log"], "tmp": tmp_path}


def with_earlier_code(core) -> None:
    earlier = core["tmp"] / "app-before"
    (earlier / "deploy").mkdir(parents=True)
    (earlier / "deploy" / "compose.yaml").write_text("services: {}\n")
    (core["tmp"] / "drive-code-before").write_text(f"{earlier}\n")


def test_undo_runs_the_earlier_code_only_behind_the_rule(core):
    with_earlier_code(core)
    answer = run(core["script"], core["env"], "undo")
    assert answer.returncode == 0, answer.stdout + answer.stderr
    calls = core["log"].read_text()
    assert "compose" in calls and "up -d --build" in calls
    assert calls.index("down") < calls.index("up -d")
    assert "--ctorigdstport 5006" in core["rules"].read_text()


def test_undo_refuses_to_start_the_earlier_code_when_the_rule_is_not_loaded(core):
    with_earlier_code(core)
    answer = run(core["script"], {**core["env"], "STUB_LIVE_RULE": "no"}, "undo")
    assert answer.returncode != 0
    assert "refusing to start drive" in answer.stdout
    assert "up -d" not in core["log"].read_text()


def test_undo_without_earlier_code_removes_the_rule_and_starts_nothing(core):
    answer = run(core["script"], core["env"], "undo")
    assert answer.returncode == 0, answer.stdout + answer.stderr
    assert "up -d" not in core["log"].read_text()
    assert core["rules"].read_text() == AFTER_RULES


def test_check_passes_only_with_the_rule_loaded_ufw_active_and_the_data_owned_by_the_container(core):
    passing = run(core["script"], core["env"], "check")
    assert passing.returncode == 0, passing.stdout
    assert "DOCKER-USER rule loaded: yes" in passing.stdout
    for change in ({"STUB_LIVE_RULE": "no"}, {"STUB_UFW": "inactive"}, {"CONTAINER_USER": "65532"}):
        failing = run(core["script"], {**core["env"], **change}, "check")
        assert failing.returncode != 0, change
        assert "core check failures: 1" in failing.stdout, (change, failing.stdout)


def test_check_fails_when_ufw_is_not_installed(core, tmp_path):
    no_ufw = tmp_path / "no-ufw"
    no_ufw.mkdir()
    for name in ("iptables", "docker"):
        (no_ufw / name).symlink_to(tmp_path / "stubs" / name)
    path = f"{no_ufw}:" + ":".join(part for part in os.environ["PATH"].split(":")
                                   if not (Path(part) / "ufw").exists())
    failing = run(core["script"], {**core["env"], "PATH": path}, "check")
    assert failing.returncode != 0
    assert "ufw: missing or inactive" in failing.stdout


def test_a_failed_ufw_reload_puts_the_rules_back_and_releases_the_lock(core):
    before = core["rules"].read_text()
    answer = run(core["script"], {**core["env"], "STUB_UFW_RELOAD": "fail"}, "remove-rule")
    assert answer.returncode != 0
    assert "reload failed" in answer.stdout
    assert core["rules"].read_text() == before
    assert not (core["tmp"] / "lock").exists()
    core["rules"].write_text(AFTER_RULES)
    added = run(core["script"], {**core["env"], "STUB_UFW_RELOAD": "fail"}, "add-rule",
                str(rendered(core["tmp"], "ufw-after.rules.drive")))
    assert added.returncode != 0
    assert core["rules"].read_text() == AFTER_RULES
    assert not (core["tmp"] / "lock").exists()


def test_the_rule_is_not_added_when_ufw_is_inactive(core):
    core["rules"].write_text(AFTER_RULES)
    answer = run(core["script"], {**core["env"], "STUB_UFW": "inactive"}, "add-rule",
                 str(rendered(core["tmp"], "ufw-after.rules.drive")))
    assert answer.returncode != 0
    assert core["rules"].read_text() == AFTER_RULES


def test_compose_names_the_project_and_publishes_only_on_the_lan_address(tmp_path):
    compose = rendered(tmp_path, "compose.yaml").read_text()
    assert compose.startswith("name: drive\n")
    assert f'"{CORE_ADDRESS}:5006:8080"' in compose
    assert "- /srv/drive-data:/data" in compose
    assert "0.0.0.0" not in compose


def test_deploy_script_is_executable_in_git():
    entry = subprocess.run(["git", "ls-files", "-s", "deploy.sh"], cwd=REPO, check=True, capture_output=True,
                           text=True).stdout
    assert entry.startswith("100755 ")


def test_the_caddy_block_carries_the_proxy_secret_and_strips_one_sent_by_visitors(tmp_path, stubs):
    script = side_script(tmp_path, "proxy")
    caddyfile = tmp_path / "Caddyfile"
    caddyfile.write_text(CADDYFILE)
    env = {**stubs, "CADDYFILE": str(caddyfile), "CADDY_LOCK": str(tmp_path / "lock"), "STATE": str(tmp_path),
           "SECRET_FILE": str(tmp_path / "missing")}
    refused = run(script, env, "add-site", str(pushed_block(tmp_path)))
    assert refused.returncode != 0
    assert caddyfile.read_text() == CADDYFILE
    assert not (tmp_path / "lock").exists()
    env["SECRET_FILE"] = str(secret_file(tmp_path))
    assert run(script, env, "add-site", str(pushed_block(tmp_path))).returncode == 0
    text = caddyfile.read_text()
    assert f"header_up X-Drive-Proxy-Secret {PROXY_SECRET}" in text
    assert "request_header -X-Drive-Proxy-Secret" in text
    share_handle = text[text.index("@share_link path /s/*"):text.index("@signed_out")]
    assert PROXY_SECRET not in share_handle


def test_the_proxy_secret_travels_from_ct_100_to_ct_109_without_being_shown(core, tmp_path, stubs):
    env_file = Path(core["env"]["CONF"]) / "drive.env"
    env_file.write_text("AUTH_MODE=proxy\n")
    assert run(core["script"], core["env"], "ensure-proxy-secret").returncode == 0
    assert run(core["script"], core["env"], "ensure-proxy-secret").returncode == 0
    lines = [line for line in env_file.read_text().splitlines() if line.startswith("PROXY_SECRET=")]
    assert len(lines) == 1 and len(lines[0].split("=", 1)[1]) == 64
    printed = run(core["script"], core["env"], "proxy-secret").stdout
    proxy = side_script(tmp_path, "proxy")
    saved_file = tmp_path / "saved-secret"
    saved = subprocess.run(["bash", str(proxy), "save-proxy-secret"], input=printed, capture_output=True, text=True,
                           env={**os.environ, "SECRET_FILE": str(saved_file)})
    assert saved.returncode == 0, saved.stdout
    assert lines[0].split("=", 1)[1] not in saved.stdout
    assert saved_file.read_text().strip() == lines[0].split("=", 1)[1]
    assert oct(saved_file.stat().st_mode & 0o777) == "0o600"
    bad = subprocess.run(["bash", str(proxy), "save-proxy-secret"], input="not-hex\n", capture_output=True,
                         text=True, env={**os.environ, "SECRET_FILE": str(saved_file)})
    assert bad.returncode != 0


def test_the_container_has_room_for_the_work_memory_budget_and_the_app():
    compose = (REPO / "deploy" / "compose.yaml").read_text()
    limit_line = next(line for line in compose.splitlines() if line.strip().startswith("mem_limit:"))
    limit_mb = int(limit_line.split(":", 1)[1].strip().removesuffix("g")) * 1024
    env = dict(line.split("=", 1) for line in (REPO / "deploy" / "drive.env").read_text().splitlines() if "=" in line)
    assert limit_mb >= int(env["WORK_MEMORY_MB"]) + APP_HEADROOM_MB



def test_the_google_client_is_saved_with_a_backup_and_never_printed(core):
    env_file = Path(core["env"]["CONF"]) / "drive.env"
    env_file.write_text("AUTH_MODE=proxy\nGOOGLE_CLIENT_ID=old.apps.googleusercontent.com\nGOOGLE_CLIENT_SECRET=old-secret\n")
    env_file.chmod(0o640)
    client_id, client_secret = "1234-abc.apps.googleusercontent.com", "GOCSPX-stub_Secret-123"
    saved = run(core["script"], core["env"], "google-set", stdin=f"{client_id}\n{client_secret}\n")
    assert saved.returncode == 0, saved.stdout
    assert client_secret not in saved.stdout + saved.stderr and client_id not in saved.stdout
    lines = env_file.read_text().splitlines()
    assert lines == ["AUTH_MODE=proxy", f"GOOGLE_CLIENT_ID={client_id}", f"GOOGLE_CLIENT_SECRET={client_secret}"]
    assert oct(env_file.stat().st_mode & 0o777) == "0o640"
    backups = list(env_file.parent.glob("drive.env.bak-drive-google-*"))
    assert len(backups) == 1 and "old-secret" in backups[0].read_text()
    assert "--force-recreate" in core["log"].read_text()


@pytest.mark.parametrize("given", ["not-a-client\nGOCSPX-stub_Secret-123\n",
                                   "1234-abc.apps.googleusercontent.com\nhas spaces in it\n",
                                   "1234-abc.apps.googleusercontent.com\n"])
def test_a_bad_google_client_changes_nothing(core, given):
    env_file = Path(core["env"]["CONF"]) / "drive.env"
    env_file.write_text("AUTH_MODE=proxy\n")
    refused = run(core["script"], core["env"], "google-set", stdin=given)
    assert refused.returncode != 0
    assert env_file.read_text() == "AUTH_MODE=proxy\n"
    assert not list(env_file.parent.glob("drive.env.bak-*"))
    assert "docker" not in core["log"].read_text()


@pytest.mark.parametrize("broken", ["not loaded", "not in the file"])
def test_setting_the_google_client_keeps_the_firewall_check(core, broken):
    env_file = Path(core["env"]["CONF"]) / "drive.env"
    env_file.write_text("AUTH_MODE=proxy\n")
    env = dict(core["env"])
    if broken == "not loaded":
        env["STUB_LIVE_RULE"] = "no"
    else:
        core["rules"].write_text(AFTER_RULES)
    client = "1234-abc.apps.googleusercontent.com\nGOCSPX-stub_Secret-123\n"
    refused = run(core["script"], env, "google-set", stdin=client)
    assert refused.returncode != 0
    assert "refusing to start drive" in refused.stdout
    assert env_file.read_text() == "AUTH_MODE=proxy\n"
    assert "up -d" not in core["log"].read_text()


def fill_in(text: str) -> str:
    for name, value in SITE_VALUES.items():
        text = text.replace(f"__{name}__", value)
    return text


def leftover_placeholders(text: str) -> set:
    return set(re.findall(r"__[A-Z_]+__", text)) - set(RUNTIME_PLACEHOLDERS)


def test_the_scripts_and_deploy_files_get_every_site_value(tmp_path):
    texts = [deploy_sh(tmp_path, f"print-{which}-script").stdout for which in ("core", "proxy")]
    texts += [rendered(tmp_path, name).read_text() for name in DEPLOY_FILES]
    for text in texts:
        assert leftover_placeholders(text) == set()
    core, proxy = texts[0], texts[1]
    assert 'DATA="${DATA:-/srv/drive-data}"' in core
    assert f"PROXY_ADDRESS={PROXY_ADDRESS}\n" in core
    assert f'UPSTREAM="${{UPSTREAM:-{CORE_ADDRESS}:5006}}"' in proxy
    assert "for other in keep tasks; do" in proxy
    assert "mail.example.com" in proxy
    assert "-H 'X-Forwarded-User: admin'" in proxy


def test_the_tracked_files_hold_no_site_values():
    tracked = (REPO / "deploy.sh").read_text() + "".join((REPO / "deploy" / name).read_text() for name in DEPLOY_FILES)
    for value in ("drive.example.com", CORE_ADDRESS, PROXY_ADDRESS, "/srv/drive-data", "4100", "4109"):
        assert value not in tracked


def test_the_laptop_side_refuses_without_the_site_file(tmp_path):
    answer = deploy_sh(tmp_path, "print-core-script", site=tmp_path / "missing.env")
    assert answer.returncode == 1
    assert "deploy/site.env.example" in answer.stdout


@pytest.mark.parametrize("name, value", [("SITE_HOST", "drive.example.com;reboot"), ("CORE_CT", ""),
                                         ("DRIVE_DATA", "relative/path"), ("DRIVE_DATA", "/srv/a|b"),
                                         ("OTHER_SITE_NAMES", "keep;tasks"), ("SIGNED_IN_USER", "admin user")])
def test_a_site_value_that_is_not_plain_is_refused(tmp_path, name, value):
    site = site_file(tmp_path, {**SITE_VALUES, name: value})
    answer = deploy_sh(tmp_path, "print-core-script", site=site)
    assert answer.returncode == 1
    assert f"refusing: {name}" in answer.stdout


def test_only_the_deploy_files_can_be_printed(tmp_path):
    answer = deploy_sh(tmp_path, "print-deploy-file", "../drive/web.py")
    assert answer.returncode == 2
    assert answer.stdout.startswith("refusing:")


def test_apply_pushes_a_code_bundle_with_the_site_values_filled_in(tmp_path):
    stubs = tmp_path / "laptop-stubs"
    stubs.mkdir()
    pushed = tmp_path / "pushed"
    pushed.mkdir()
    (stubs / "scp").write_text('#!/bin/sh\necho "scp $*" >> "$STUB_LOG"\ncp "$2" "$PUSHED/$(basename "${3#*:}")"\n')
    (stubs / "ssh").write_text('#!/bin/sh\necho "ssh $*" >> "$STUB_LOG"\n')
    builder = tmp_path / "signin"
    builder.mkdir()
    (builder / "build.py").write_text("import pathlib, sys\nout = pathlib.Path(sys.argv[sys.argv.index('--out') + 1])\n"
                                      "out.mkdir(parents=True)\n(out / 'index.html').write_text('sign in')\n")
    for stub in stubs.iterdir():
        stub.chmod(0o755)
    log = tmp_path / "laptop.log"
    env = {"PATH": f"{stubs}:{os.environ['PATH']}", "STUB_LOG": str(log), "PUSHED": str(pushed), "SITE_SIGNIN": str(builder)}
    answer = deploy_sh(tmp_path, "apply", "HEAD", env=env)
    assert answer.returncode == 0, answer.stdout + answer.stderr
    with tarfile.open(pushed / "drive-code.tgz") as bundle:
        names = bundle.getnames()
        for name in DEPLOY_FILES:
            member = bundle.getmember(f"deploy/{name}")
            assert (member.uid, member.gid, member.mode) == (0, 0, 0o664)
            text = bundle.extractfile(member).read().decode()
            committed = subprocess.run(["git", "show", f"HEAD:deploy/{name}"], cwd=REPO, check=True, capture_output=True,
                                       text=True).stdout
            assert text == fill_in(committed)
            assert leftover_placeholders(text) == set()
    assert len(names) == len(set(names))
    assert "deploy.sh" in names
    block = (pushed / "drive-Caddyfile.block").read_text()
    committed_block = subprocess.run(["git", "show", "HEAD:deploy/Caddyfile.drive"], cwd=REPO, check=True,
                                     capture_output=True, text=True).stdout
    assert block == fill_in(committed_block)
    assert leftover_placeholders(block) == set() and "__UPSTREAM__" in block
    assert "pct exec 4100 -- bash /root/drive-deploy-core.sh apply /root/drive-code.tgz" in log.read_text()


def test_the_caddy_refusal_names_starfire_drive():
    block = (REPO / "deploy" / "Caddyfile.drive").read_text()
    assert 'respond "Starfire Drive is only for the admin account, so this account cannot open it. Sign in with the admin account instead, or ask the admin for what you need." 403' in block


def test_the_sign_in_page_is_built_as_starfire_drive():
    assert "--first Starfire --second Drive --tagline" in (REPO / "deploy.sh").read_text()


def test_an_existing_block_gets_the_current_refusal_and_nothing_else_changes(tmp_path, stubs):
    script = side_script(tmp_path, "proxy")
    caddyfile = tmp_path / "Caddyfile"
    old_refusal = 'respond "Drive is for the admin account only." 403'
    other_site = f"http://other.example.com {{\n\t{old_refusal}\n}}\n"
    caddyfile.write_text(CADDYFILE + "\n" + other_site)
    env = {**stubs, "CADDYFILE": str(caddyfile), "CADDY_LOCK": str(tmp_path / "lock"), "STATE": str(tmp_path),
           "SECRET_FILE": str(secret_file(tmp_path))}
    assert run(script, env, "add-site", str(pushed_block(tmp_path))).returncode == 0
    current = caddyfile.read_text()
    marked = current.index("# >>> drive block")
    caddyfile.write_text(current[:marked] + current[marked:].replace(
        'respond "Starfire Drive is only for the admin account, so this account cannot open it. Sign in with the admin account instead, or ask the admin for what you need." 403', old_refusal))
    stubs["log"].write_text("")
    answer = run(script, env, "add-site", str(pushed_block(tmp_path)))
    assert answer.returncode == 0, answer.stdout + answer.stderr
    assert caddyfile.read_text() == current
    assert caddyfile.read_text().count(old_refusal) == 1
    assert "systemctl reload caddy" in stubs["log"].read_text()
    assert any(old_refusal in backup.read_text()[backup.read_text().index("# >>> drive block"):]
               for backup in tmp_path.glob("Caddyfile.bak-drive-*"))
    assert not (tmp_path / "lock").exists()
    stubs["log"].write_text("")
    assert run(script, env, "add-site", str(pushed_block(tmp_path))).returncode == 0
    assert caddyfile.read_text() == current
    assert "systemctl reload caddy" not in stubs["log"].read_text()


def test_a_refusal_that_does_not_validate_leaves_the_existing_block_alone(tmp_path, stubs):
    script = side_script(tmp_path, "proxy")
    caddyfile = tmp_path / "Caddyfile"
    caddyfile.write_text(CADDYFILE)
    env = {**stubs, "CADDYFILE": str(caddyfile), "CADDY_LOCK": str(tmp_path / "lock"), "STATE": str(tmp_path),
           "SECRET_FILE": str(secret_file(tmp_path))}
    assert run(script, env, "add-site", str(pushed_block(tmp_path))).returncode == 0
    before = caddyfile.read_text()
    broken = pushed_block(tmp_path)
    broken.write_text(broken.read_text().replace("Starfire Drive is", "BROKEN Drive is"))
    stubs["log"].write_text("")
    assert run(script, env, "add-site", str(broken)).returncode != 0
    assert caddyfile.read_text() == before
    assert "systemctl reload caddy" not in stubs["log"].read_text()
    assert not (tmp_path / "lock").exists()


def test_the_caddy_refusal_says_who_the_app_is_for_and_what_to_do_instead():
    refusal = re.search(r'respond "([^"]+)" 403', (REPO / "deploy" / "Caddyfile.drive").read_text()).group(1)
    assert refusal.startswith("Starfire Drive is only for the admin account")
    assert "Sign in with the admin account instead" in refusal
    assert "ask the admin" in refusal
    assert len(refusal) < 200
