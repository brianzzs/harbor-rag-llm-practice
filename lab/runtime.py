"""Run the source server in process for smoke and acceptance checks."""

from contextlib import contextmanager
from threading import Thread

from .source import SourceServer


@contextmanager
def running_source():
    with SourceServer(0) as server:
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}"
        finally:
            server.shutdown()
            thread.join()
