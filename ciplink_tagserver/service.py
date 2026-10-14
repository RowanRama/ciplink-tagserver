"""Runs the tag server at Windows startup as a Task Scheduler task (no extra software needed).

The task runs `ciplink-tagserver service run`, which serves until stopped and starts the server again
if it ever returns or raises. Installing and removing need an elevated (administrator) terminal.
"""

import logging
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from xml.sax.saxutils import escape

log = logging.getLogger(__name__)

RESTART_DELAY = 5  # seconds between a server exit and the next start

TASK_XML = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>ciplink-tagserver: serves PLC tag values to React apps</Description>
  </RegistrationInfo>
  <Triggers>
    <BootTrigger><Enabled>true</Enabled></BootTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>S-1-5-18</UserId>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <RestartOnFailure><Interval>PT1M</Interval><Count>999</Count></RestartOnFailure>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{command}</Command>
      <Arguments>{arguments}</Arguments>
      <WorkingDirectory>{workdir}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def task_xml(config: Path, log_file: Path, python: str = sys.executable) -> str:
    arguments = f'-m ciplink_tagserver.cli service run --config "{config}" --log-file "{log_file}"'
    return TASK_XML.format(command=escape(python), arguments=escape(arguments), workdir=escape(str(config.parent)))


def schtasks(*args) -> subprocess.CompletedProcess:
    return subprocess.run(["schtasks", *args], capture_output=True, text=True)


def install(name: str, config: Path, log_file: Path) -> int:
    with tempfile.TemporaryDirectory() as tmp:
        xml = Path(tmp) / "task.xml"
        xml.write_text(task_xml(config, log_file), encoding="utf-16")
        result = schtasks("/Create", "/TN", name, "/XML", str(xml), "/F")
    if result.returncode != 0:
        print(result.stderr.strip() or result.stdout.strip())
        print("Installing needs an administrator terminal: right-click Terminal, 'Run as administrator'.")
        return result.returncode
    schtasks("/Run", "/TN", name)
    print(f"Installed and started task '{name}'. It starts at every boot; logs go to {log_file}")
    return 0


def remove(name: str) -> int:
    schtasks("/End", "/TN", name)
    result = schtasks("/Delete", "/TN", name, "/F")
    print((result.stdout or result.stderr).strip())
    return result.returncode


def status(name: str) -> int:
    result = schtasks("/Query", "/TN", name, "/V", "/FO", "LIST")
    print((result.stdout or result.stderr).strip())
    return result.returncode


def supervise(serve, stop=lambda: False, sleep=time.sleep):
    """Calls serve() again whenever it returns or raises, until stop() is true. Ctrl+C ends it."""
    while not stop():
        try:
            serve()
            log.warning("tag server stopped, starting it again in %s s", RESTART_DELAY)
        except KeyboardInterrupt:
            raise
        except BaseException as err:  # SystemExit from uvicorn (e.g. port in use) included
            log.error("tag server failed: %r; starting it again in %s s", err, RESTART_DELAY)
        sleep(RESTART_DELAY)
