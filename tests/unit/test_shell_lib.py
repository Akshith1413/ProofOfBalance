"""scripts/lib/common.sh, exercised through bash."""
import subprocess

import pytest

from conftest import ROOT

LIB = ROOT / "scripts" / "lib" / "common.sh"


def bash(script: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "-c", f"set -Eeuo pipefail; source '{LIB}'; {script}"],
                          capture_output=True, text=True, timeout=60, env=env or {"PATH": "/usr/bin:/bin"})


def test_load_env_parses_quotes_comments_and_export(tmp_path):
    f = tmp_path / ".env"
    f.write_text(
        "# comment\n\n"
        "A=plain\n"
        "export B=exported\n"
        'C="double # not a comment"\n'
        "D='single'\n"
        "E=trailing   # comment\n"
        "F=aB/c//dE+fG=\n"
        "G=\n"
        "not a valid line\n"
        "H=crlf\r\n"
    )
    r = bash(f"load_env '{f}'; printf '%s|' \"$A\" \"$B\" \"$C\" \"$D\" \"$E\" \"$F\" \"$G\" \"$H\"")
    assert r.returncode == 0, r.stderr
    assert r.stdout == "plain|exported|double # not a comment|single|trailing|aB/c//dE+fG=||crlf|"
    assert "malformed" in r.stderr


def test_load_env_never_executes_code(tmp_path):
    f = tmp_path / ".env"
    marker = tmp_path / "pwned"
    f.write_text(f"X=$(touch {marker})\nY=`touch {marker}`\n")
    r = bash(f"load_env '{f}'; echo \"$X\"")
    assert r.returncode == 0 and not marker.exists()
    assert r.stdout.strip() == f"$(touch {marker})"


def test_load_env_precedence_empty_does_not_block_later_file(tmp_path):
    env_file, generated = tmp_path / ".env", tmp_path / "generated.env"
    env_file.write_text("PW=\nKEEP=from-env-file\n")
    generated.write_text("PW=generated-secret\nKEEP=from-generated\n")
    r = bash(f"load_env '{env_file}'; load_env '{generated}'; echo \"$PW $KEEP\"")
    assert r.stdout.strip() == "generated-secret from-env-file"


def test_shell_environment_wins_over_file(tmp_path):
    f = tmp_path / ".env"
    f.write_text("AWS_REGION=ap-south-1\n")
    r = bash(f"load_env '{f}'; echo \"$AWS_REGION\"", env={"PATH": "/usr/bin:/bin", "AWS_REGION": "eu-west-1"})
    assert r.stdout.strip() == "eu-west-1"


def test_load_env_missing_file_returns_nonzero():
    assert bash("load_env /nonexistent || echo missing").stdout.strip() == "missing"


def test_random_secret_is_32_alnum_and_survives_pipefail():
    r = bash("a=$(random_secret); b=$(random_secret); echo \"$a $b\"")
    a, b = r.stdout.split()
    assert r.returncode == 0 and len(a) == 32 and a.isalnum() and a != b


def test_retry_eventually_succeeds_and_gives_up(tmp_path):
    counter = tmp_path / "n"
    counter.write_text("0")
    script = (f"bump() {{ n=$(($(cat {counter}) + 1)); echo $n > {counter}; [[ $n -ge 3 ]]; }}; "
              "retry 5 0 bump && echo ok")
    assert bash(script).stdout.strip().endswith("ok") and counter.read_text().strip() == "3"
    assert bash("retry 2 0 false || echo gave-up").stdout.strip().endswith("gave-up")


def test_probe_until_is_quiet_on_timeout():
    r = bash("probe_until 1 1 false || echo timed-out")
    assert r.stdout.strip() == "timed-out" and "[fail]" not in r.stdout + r.stderr


def test_stage_failure_writes_status_report(tmp_path):
    script = (f"LOG_DIR='{tmp_path}'; init_logging t; "
              "boom() { echo working; false; }; stage 'Do thing' 'Explains what to do.' boom")
    r = bash(script)
    assert r.returncode != 0
    status = (tmp_path / "t-status.txt").read_text()
    assert "RESULT: FAILED" in status and "Failed stage : Do thing" in status
    assert "Explains what to do." in status and "Failing command: 'false'" in status
    assert "working" in status  # last log lines included


def test_success_status_report(tmp_path):
    r = bash(f"LOG_DIR='{tmp_path}'; init_logging t; ok_fn() {{ summary 'hello'; }}; stage 'Fine' 'n/a' ok_fn")
    assert r.returncode == 0
    status = (tmp_path / "t-status.txt").read_text()
    assert status.startswith("RESULT: SUCCESS") and "Fine (" in status and "hello" in status


def test_persist_generated_replaces_values(tmp_path):
    r = bash(f"SECRETS_DIR='{tmp_path}'; persist_generated K one; persist_generated K two; persist_generated J x; "
             f"cat '{tmp_path}/generated.env'")
    assert r.stdout.splitlines() == ["K=two", "J=x"]
    assert oct((tmp_path / "generated.env").stat().st_mode)[-3:] == "600"


@pytest.mark.parametrize("answer,expected", [("y", 0), ("yes", 0), ("n", 1), ("", 1)])
def test_confirm_requires_tty_or_yes(answer, expected):
    assert bash("ASSUME_YES=true confirm 'go?'").returncode == 0
    assert bash("confirm 'go?'").returncode != 0  # no terminal -> refuse, never assume yes


def test_tunnel_socket_path_is_short_even_for_deep_checkouts():
    import pathlib, shutil, tempfile
    tmp_path = pathlib.Path(tempfile.mkdtemp(prefix="rt", dir="/tmp"))  # a short runtime dir, like /run/user/1000
    tunnel = ROOT / "scripts" / "lib" / "tunnel.sh"
    deep_a, deep_b = "/x" * 80 + "/a/.secrets", "/x" * 80 + "/b/.secrets"
    env = {"PATH": "/usr/bin:/bin", "XDG_RUNTIME_DIR": str(tmp_path)}
    socks = [bash(f"SECRETS_DIR='{d}'; source '{tunnel}'; echo \"$TUNNEL_SOCK\"", env=env).stdout.strip() for d in (deep_a, deep_b)]
    assert all(s.startswith(str(tmp_path)) for s in socks) and socks[0] != socks[1]
    assert all(len(s) + 17 < 104 for s in socks)  # ssh appends ".XXXXXXXXXXXXXXXX" while binding
    assert oct((tmp_path / socks[0].split("/")[-2]).stat().st_mode)[-3:] == "700"
    shutil.rmtree(tmp_path)
