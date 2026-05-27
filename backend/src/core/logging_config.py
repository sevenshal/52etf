import logging
import sys
from typing import TextIO


LOG_FORMAT = "%(asctime)s [%(process)d] [%(threadName)s] %(levelname)s %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def configure_logging(stdout: TextIO = None, stderr: TextIO = None):
    stdout_handler = logging.StreamHandler(stdout or sys.stdout)
    stdout_handler.setLevel(logging.INFO)

    stderr_handler = logging.StreamHandler(stderr or sys.stderr)
    stderr_handler.setLevel(logging.ERROR)

    logging.basicConfig(
        level=logging.INFO,
        format=LOG_FORMAT,
        datefmt=LOG_DATE_FORMAT,
        handlers=[stdout_handler, stderr_handler],
        force=True,
    )
