import os

import pytest

from law_agent.review import python_sandbox


@pytest.fixture(scope="module")
def sandbox():
    if not os.getenv("LAWAGENT_WASI_PYTHON_ROOT"):
        pytest.skip("requires the CPython WASI runtime")
    return python_sandbox.sandbox_from_env()


def test_guest_processes_frozen_text_and_isolates_failures(sandbox):
    computed = sandbox.run(
        "import csv, io\nprint(sum(int(row[1]) for row in csv.reader(io.StringIO(material))))",
        "甲,13\n乙,29",
    )
    assert computed["ok"] and computed["stdout"] == "42\n"
    failure = sandbox.run("print(42)\nraise ValueError('bad input')", "")
    assert not failure["ok"]
    assert "stdout" not in failure
    assert failure["diagnostic_stdout"] == "42\n"
    assert "bad input" in failure["stderr"]
    next_run = sandbox.run("print(material)", "新材料")
    assert next_run["ok"] and next_run["stdout"] == "新材料\n"


def test_guest_cannot_read_host_or_access_network(sandbox, tmp_path, monkeypatch):
    secret = tmp_path / "secret"
    secret.write_text("host-only", encoding="utf-8")
    monkeypatch.setenv("HOST_ONLY_SECRET", "host-only")
    code = f"""
import os
assert "HOST_ONLY_SECRET" not in os.environ
try:
    open({str(secret)!r}).read()
except OSError:
    print("file blocked")
try:
    open("/runtime/probe", "w")
except OSError:
    print("write blocked")
try:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
        client.connect(("1.1.1.1", 443))
except (ImportError, OSError, AttributeError):
    print("network blocked")
"""
    result = sandbox.run(code, "")
    assert result["ok"] and result["stdout"] == "file blocked\nwrite blocked\nnetwork blocked\n", (
        result
    )


def test_guest_timeout_and_bounded_output(sandbox, monkeypatch):
    result = sandbox.run("print('x' * 6000)", "")
    assert result["ok"] and result["truncated"]
    assert len(result["stdout"]) == python_sandbox.MAX_OUTPUT_BYTES
    monkeypatch.setattr(python_sandbox, "TIMEOUT_SECONDS", 2)
    timeout = sandbox.run("while True: pass", "")
    assert not timeout["ok"] and "超时" in timeout["error"]
