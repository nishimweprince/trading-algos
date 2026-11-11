from __future__ import annotations

import json
import threading
import urllib.request

from solcarry.ui.server import list_runs, make_server


def test_server(tmp_path):
    run = tmp_path / "20260101T000000Z-abc"
    run.mkdir()
    (run / "ui.json").write_text(json.dumps({"x": 1}))
    (run / "gate.json").write_text(json.dumps({"result": "KILL", "unlocked": True}))
    (tmp_path / "secret.txt").write_text("no")
    assert list_runs(tmp_path)[0]["result"] == "KILL"
    srv = make_server(tmp_path, "127.0.0.1", 0)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        base = f"http://127.0.0.1:{port}"
        assert b"solcarry" in urllib.request.urlopen(base + "/").read()
        runs = json.loads(urllib.request.urlopen(base + "/api/runs").read())
        assert runs[0]["run_id"] == run.name
        assert json.loads(urllib.request.urlopen(f"{base}/runs/{run.name}/ui.json").read()) == {
            "x": 1
        }
        try:
            urllib.request.urlopen(f"{base}/runs/..%2F/secret.txt")
            raise AssertionError("traversal should 404")
        except urllib.error.HTTPError as e:
            assert e.code == 404
    finally:
        srv.shutdown()
        srv.server_close()
