"""#507: `finresearch serve` must not start a second copy. uvicorn runs the app's lifespan (the monitor scheduler)
before it binds the port, so a second `serve` used to run a second scheduler for a moment, which could claim
tonight's jobs, and only then fail on "address already in use"."""

from __future__ import annotations

import socket

from typer.testing import CliRunner


def test_serve_refuses_a_port_that_is_already_in_use_before_starting_anything(monkeypatch):
    import uvicorn

    from finresearch import api
    from finresearch.cli import app

    started: list[str] = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: started.append("uvicorn"))
    monkeypatch.setattr(api, "create_app", lambda **kw: started.append("app"))
    with socket.socket() as held:  # the copy that is already running: this test's own listener
        held.bind(("127.0.0.1", 0))
        held.listen(1)
        port = held.getsockname()[1]
        r = CliRunner().invoke(app, ["serve", "--port", str(port)])
        assert r.exit_code == 1 and started == [], (
            r.output,
            started,
        )  # before #507: the app and uvicorn started
        assert f"127.0.0.1:{port} is already in use" in r.output and "Not starting a second copy" in r.output
    r = CliRunner().invoke(app, ["serve", "--port", str(port)])  # the port is free again: it starts as before
    assert r.exit_code == 0 and started == ["app", "uvicorn"], r.output
