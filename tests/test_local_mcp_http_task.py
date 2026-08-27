from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from scripts.run_local_mcp_http_task import build_argv


class LocalMcpHttpTaskTests(unittest.TestCase):
    def test_project_source_runtime_uses_a_port_separate_from_control_plane(self) -> None:
        config = Path(__file__).resolve().parents[1] / "config" / "local-mcp-http-community-source.json"
        argv = build_argv(config)
        port_index = argv.index("--port")
        self.assertEqual(argv[port_index + 1], "18766")

    def test_build_argv_preserves_read_roots(self) -> None:
        with TemporaryDirectory() as tmp:
            config = Path(tmp) / "task.json"
            config.write_text(
                json.dumps(
                    {
                        "log_file": "C:/logs/mcp.log",
                        "server_args": [
                            "--workspace", "D:/",
                            "--read-root", "C:/Users/example/AppData",
                            "--read-root", "C:/Users/example/Documents/source",
                        ],
                    }
                ),
                encoding="utf-8",
            )

            argv = build_argv(config)

            self.assertEqual(
                argv,
                [
                    "--log-file", "C:/logs/mcp.log", "--",
                    "--workspace", "D:/",
                    "--read-root", "C:/Users/example/AppData",
                    "--read-root", "C:/Users/example/Documents/source",
                ],
            )

    def test_build_argv_rejects_invalid_config(self) -> None:
        payloads = [
            {},
            {"log_file": "x"},
            {"log_file": "x", "server_args": []},
            {"log_file": "", "server_args": ["--workspace", "D:/"]},
            {"log_file": "x", "server_args": ["--workspace", 1]},
            {"log_file": "x", "server_args": ["--workspace"], "extra": True},
        ]
        for payload in payloads:
            with self.subTest(payload=payload), TemporaryDirectory() as tmp:
                config = Path(tmp) / "task.json"
                config.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaises(ValueError):
                    build_argv(config)


if __name__ == "__main__":
    unittest.main()
