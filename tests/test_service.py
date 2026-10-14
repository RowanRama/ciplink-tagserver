import logging
from pathlib import Path
from types import SimpleNamespace
from xml.etree import ElementTree

from ciplink_tagserver import cli, service

NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}


def test_task_xml_runs_the_supervisor_at_boot_as_system():
    config = Path(r"C:\Tag Server\tagserver.toml")
    xml = service.task_xml(config, config.with_name("tagserver.log"), python=r"C:\Py & Co\python.exe")
    root = ElementTree.fromstring(xml.split("\n", 1)[1])  # skip the UTF-16 declaration for the parser
    assert root.find("t:Triggers/t:BootTrigger", NS) is not None
    assert root.find("t:Principals/t:Principal/t:UserId", NS).text == "S-1-5-18"
    assert root.find("t:Settings/t:ExecutionTimeLimit", NS).text == "PT0S"
    exec_ = root.find("t:Actions/t:Exec", NS)
    assert exec_.find("t:Command", NS).text == r"C:\Py & Co\python.exe"  # escaped in the XML, intact once parsed
    assert exec_.find("t:Arguments", NS).text == (
        r'-m ciplink_tagserver.cli service run --config "C:\Tag Server\tagserver.toml" '
        r'--log-file "C:\Tag Server\tagserver.log"'
    )
    assert exec_.find("t:WorkingDirectory", NS).text == r"C:\Tag Server"


def test_install_creates_and_starts_the_task(monkeypatch, tmp_path):
    calls = []

    def fake(*args):
        calls.append(args)
        if args[0] == "/Create":
            assert Path(args[4]).read_text(encoding="utf-16").startswith('<?xml version="1.0" encoding="UTF-16"?>')
        return SimpleNamespace(returncode=0, stdout="SUCCESS", stderr="")

    monkeypatch.setattr(service, "schtasks", fake)
    assert service.install("ts", tmp_path / "c.toml", tmp_path / "c.log") == 0
    assert [c[0] for c in calls] == ["/Create", "/Run"]
    assert calls[0][:3] == ("/Create", "/TN", "ts") and calls[0][-1] == "/F"


def test_install_without_admin_reports_it(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(service, "schtasks", lambda *a: SimpleNamespace(returncode=1, stdout="", stderr="ERROR: Access is denied."))
    assert service.install("ts", tmp_path / "c.toml", tmp_path / "c.log") == 1
    out = capsys.readouterr().out
    assert "Access is denied" in out and "administrator" in out


def test_supervise_restarts_after_a_return_or_a_failure():
    runs, sleeps = [], []

    def serve():
        runs.append(1)
        if len(runs) == 1:
            raise RuntimeError("boom")
        if len(runs) == 2:
            raise SystemExit(3)  # what uvicorn does when the port is taken

    service.supervise(serve, stop=lambda: len(runs) >= 3, sleep=sleeps.append)
    assert len(runs) == 3 and sleeps == [service.RESTART_DELAY] * 3


def test_service_run_supervises_the_server_with_a_log_file(monkeypatch, tmp_path):
    config = tmp_path / "tagserver.toml"
    seen = {}
    monkeypatch.setattr(cli, "setup_logging", lambda log_file=None: seen.setdefault("log", log_file))
    monkeypatch.setattr(cli, "run", lambda args: seen.update(config=args.config, log_file=args.log_file, mock=args.mock))
    monkeypatch.setattr(service, "supervise", lambda serve: serve())
    cli.main(["service", "run", "--config", str(config)])
    assert seen["config"] == str(config.resolve())
    assert seen["log_file"] == str(config.with_name("tagserver.log").resolve())
    assert seen["mock"] is False


def test_log_file_rotates(tmp_path):
    root = logging.getLogger()
    old = root.handlers[:]
    root.handlers.clear()
    try:
        cli.setup_logging(tmp_path / "x.log")
        assert isinstance(root.handlers[0], logging.handlers.RotatingFileHandler)
        assert root.handlers[0].maxBytes == 10_000_000
    finally:
        for handler in root.handlers:
            handler.close()
        root.handlers[:] = old
