"""Opt-in service permission: real JS rule and sandboxed shell filesystem tests.

No tests invoke sudo, polkit, systemd, SSH or vehicle I/O. Node executes the
rule against fake action/subject objects, just as dashboard JS tests use Node.
The installer functions run with temporary paths and mocked root ownership.
"""

import itertools
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts/enable_agent_service_control.sh"
RULE = REPO / "scripts/polkit/49-hummer-obd-recorder.rules"
NODE = shutil.which("node") or shutil.which("nodejs")


def shell(body, *args, node_override=False):
    return subprocess.run(
        ["bash", "-c", f"source {shlex.quote(str(SCRIPT))}\n{body}", "test", *map(str, args)],
        capture_output=True, text=True, timeout=10,
        env={**os.environ, "HUMMER_I_AM_THE_NODE": "1" if node_override else ""},
    )


@pytest.mark.skipif(not NODE, reason="Node.js needed to execute polkit JavaScript")
def test_real_rule_grants_only_exact_user_unit_and_start_stop():
    actions = ["org.freedesktop.systemd1.manage-units", "org.freedesktop.systemd1.manage-unit-files",
               "org.freedesktop.systemd1.reload-daemon", "org.freedesktop.policykit.exec", None]
    users = ["jeremy", "root", "other", "jeremy-other", None]
    units = ["hummer-drive.service", "hummer-drive", "hummer-drive@evil.service",
             "hummer-drive.service.evil", "hummer-dashboard.service", "ssh.service", "*", None]
    verbs = ["start", "stop", "restart", "try-restart", "reload", "kill", "set-property",
             "reset-failed", "enable", "disable", "start;reboot", "START", None]
    cases = [dict(action=a, user=u, unit=n, verb=v)
             for a, u, n, v in itertools.product(actions, users, units, verbs)]
    program = r"""
const fs = require('fs'), vm = require('vm');
const callbacks = [];
vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), {
  polkit: {addRule: callback => callbacks.push(callback), Result: {YES: 'yes'}}
});
if (callbacks.length !== 1) throw new Error('expected exactly one rule');
const cases = JSON.parse(fs.readFileSync(0, 'utf8'));
console.log(JSON.stringify(cases.map(c => {
  const result = callbacks[0](
    {id: c.action, lookup: key => c[key]}, {user: c.user, active: false, local: false}
  );
  return typeof result === 'undefined' ? 'fallthrough' : result;
})));
"""
    result = subprocess.run([NODE, "-e", program, str(RULE)], input=json.dumps(cases),
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    # No truthiness/coercion, wildcard, prefix or active-local-session shortcut.
    expected = ["yes" if (c["action"] == actions[0] and c["user"] == "jeremy"
                          and c["unit"] == "hummer-drive.service"
                          and c["verb"] in ("start", "stop")) else "fallthrough" for c in cases]
    assert json.loads(result.stdout) == expected
    assert expected.count("yes") == 2


# No command in this harness is allowed to reach a real system service. The
# real file mode is retained; only ownership and chown require simulation.
FILESYSTEM_STUBS = r"""
install() {
    [[ "$1 $2 $3 $4 $5 $6 $7" == '-o root -g root -m 0644 --' ]] || exit 91
    command install -m 0644 -- "$8" "$9"
}
stat() {
    [[ "$1 $2" == '-c %u:%g:%a' ]] || exit 92
    printf '0:0:%s\n' "$(command stat -c %a "$3")"
}
"""


def test_install_publishes_complete_rule_idempotently_and_cleans_staging(tmp_path):
    dest = tmp_path / RULE.name
    for _ in range(2):
        result = shell(FILESYSTEM_STUBS + '\ninstall_rule "$1" "$2"', RULE, dest)
        assert result.returncode == 0, result.stderr
        assert dest.read_bytes() == RULE.read_bytes()
        assert dest.stat().st_mode & 0o777 == 0o644
        assert list(tmp_path.iterdir()) == [dest]
    assert "already installed" in result.stdout


@pytest.mark.parametrize("kind", ["different", "symlink", "dangling", "directory", "fifo", "mode"])
def test_existing_policy_is_never_overwritten_or_chmodded(tmp_path, kind):
    dest = tmp_path / RULE.name
    if kind == "different":
        dest.write_text("unrelated policy")
    elif kind == "symlink":
        dest.symlink_to(RULE)
    elif kind == "dangling":
        dest.symlink_to(tmp_path / "missing")
    elif kind == "directory":
        dest.mkdir()
    elif kind == "fifo":
        os.mkfifo(dest)
    else:
        dest.write_bytes(RULE.read_bytes())
        dest.chmod(0o600)
    before = dest.lstat()
    result = shell(FILESYSTEM_STUBS + '\ninstall_rule "$1" "$2"', RULE, dest)
    assert result.returncode != 0
    assert dest.lstat() == before
    assert list(tmp_path.iterdir()) == [dest]


@pytest.mark.parametrize("failure", ["copy", "publish", "collision"])
def test_failed_install_leaves_no_partial_active_rule_or_staging(tmp_path, failure):
    dest = tmp_path / RULE.name
    override = {
        "copy": "install() { return 8; }",
        "publish": "ln() { return 9; }",
        "collision": 'ln() { printf "raced policy" > "${@: -1}"; command ln "$@"; }',
    }[failure]
    result = shell(FILESYSTEM_STUBS + "\n" + override + '\ninstall_rule "$1" "$2"', RULE, dest)
    assert result.returncode != 0
    if failure == "collision":
        assert dest.read_text() == "raced policy"
        assert list(tmp_path.iterdir()) == [dest]
    else:
        assert list(tmp_path.iterdir()) == []


def test_changed_source_cannot_publish_an_unreviewed_rule(tmp_path):
    source = tmp_path / "source.rules"
    source.write_text('polkit.addRule(function() { return "yes"; });')
    dest = tmp_path / RULE.name
    result = shell(FILESYSTEM_STUBS + '\ninstall_rule "$1" "$2"', source, dest)
    assert result.returncode != 0
    assert "reviewed policy" in result.stderr
    assert list(tmp_path.iterdir()) == [source]


def test_staged_ownership_is_verified_before_publish(tmp_path):
    result = shell(FILESYSTEM_STUBS + '\nstat() { echo 1000:1000:644; }\n'
                   'install_rule "$1" "$2"', RULE, tmp_path / RULE.name)
    assert result.returncode != 0
    assert list(tmp_path.iterdir()) == []


PREREQUISITE_STUBS = r"""
id() { [[ "$*" == '-u jeremy' ]] || exit 93; echo 1000; }
systemctl() {
    case "$*" in
        'show --property=LoadState --value hummer-drive.service') echo loaded ;;
        'show --property=Id --value hummer-drive.service') echo hummer-drive.service ;;
        'show --property=User --value hummer-drive.service') echo jeremy ;;
        'is-active --quiet polkit.service') return 0 ;;
        *) echo "unexpected systemctl: $*" >&2; exit 94 ;;
    esac
}
pkaction() { echo org.freedesktop.systemd1.manage-units; }
stat() {
    case "$2" in
        %u) echo 0 ;;
        %a) echo 755 ;;
        *) exit 95 ;;
    esac
}
"""


def test_prerequisites_are_read_only(tmp_path):
    result = shell(PREREQUISITE_STUBS + '\ncheck_prerequisites "$1" "$2"', RULE, tmp_path)
    assert result.returncode == 0, result.stderr
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("override", [
    "id() { return 1; }", "id() { echo 0; }", "id() { echo bad; }",
    "systemctl() { return 1; }", "systemctl() { echo not-found; }",
    "pkaction() { return 1; }", "pkaction() { echo unrelated-action; }",
    "stat() { echo 1000; }", "stat() { [[ $2 == %u ]] && echo 0 || echo 777; }",
    "command() { [[ $1 == -v && $2 == pkaction ]] && return 1; builtin command \"$@\"; }",
])
def test_prerequisite_failure_does_not_write(tmp_path, override):
    result = shell(PREREQUISITE_STUBS + "\n" + override + '\ncheck_prerequisites "$1" "$2"',
                   RULE, tmp_path)
    assert result.returncode != 0
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("kind", ["missing-source", "symlink-source", "symlink-directory"])
def test_missing_or_symlinked_inputs_are_refused(tmp_path, kind):
    source = RULE
    directory = tmp_path
    if kind == "missing-source":
        source = tmp_path / "missing"
    elif kind == "symlink-source":
        source = tmp_path / "source.rules"
        source.symlink_to(RULE)
    else:
        directory = tmp_path / "rules.d"
        directory.symlink_to(tmp_path)
    before = set(tmp_path.iterdir())
    result = shell(PREREQUISITE_STUBS + '\ncheck_prerequisites "$1" "$2"', source, directory)
    assert result.returncode != 0
    assert set(tmp_path.iterdir()) == before


def test_cli_invalid_args_wrong_host_and_nonroot_refuse_before_install():
    result = shell('main --destination /tmp/not-allowed')
    assert result.returncode != 0
    assert "usage" in result.stderr
    # The common require-node tests cover the physical host check for real.
    if not Path("/proc/device-tree/model").exists():
        result = shell("main --check")
        assert result.returncode != 0
        assert "Nothing has been changed" in result.stderr
    if os.geteuid() != 0:
        result = shell("main", node_override=True)
        assert result.returncode != 0
        assert "one-time installation requires root" in result.stderr


def test_check_mode_cannot_install_even_if_prerequisites_pass():
    result = shell('check_prerequisites() { :; }\n'
                   'install_rule() { echo UNEXPECTED_INSTALL; exit 99; }\nmain --check',
                   node_override=True)
    assert result.returncode == 0, result.stderr
    assert "did not install" in result.stdout
    assert "UNEXPECTED_INSTALL" not in result.stdout
