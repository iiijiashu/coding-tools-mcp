from __future__ import annotations

import json
import unittest
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from scripts.run_local_mcp_http_task import (
    build_argv,
    build_argv_from_config,
    load_config_snapshot,
    require_config_hash_pin,
    require_windows_administrator,
    verify_config_hash,
    verify_config_snapshot,
)


class LocalMcpHttpTaskTests(unittest.TestCase):
    def test_production_config_pins_explicit_privileged_contract(self) -> None:
        config = Path(__file__).resolve().parents[1] / "config" / "local-mcp-http-task.json"
        payload = json.loads(config.read_text(encoding="utf-8"))
        args = payload["server_args"]

        self.assertIs(payload["require_config_sha256_pin"], True)
        self.assertIs(payload["require_windows_administrator"], True)
        self.assertTrue(payload["auth_header_file"])
        self.assertEqual(args[args.index("--permission-mode") + 1], "dangerous")
        self.assertIn("--dangerously-allow-any-local-path", args)
        self.assertNotIn("--dangerously-fake-readonly-annotations", args)
        self.assertIn("--enable-computer-use", args)

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

    def test_build_argv_loads_bearer_token_from_header_file_without_putting_it_in_config(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            header_file = root / "backend-authorization.txt"
            header_file.write_text("Bearer test-secret-value\n", encoding="utf-8")
            config = root / "task.json"
            config.write_text(
                json.dumps(
                    {
                        "log_file": "C:/logs/mcp.log",
                        "server_args": ["--workspace", "D:/coding-tools-mcp"],
                        "auth_header_file": str(header_file),
                    }
                ),
                encoding="utf-8",
            )

            argv = build_argv(config)

            self.assertEqual(argv[-2:], ["--auth-token", "test-secret-value"])
            self.assertNotIn("Bearer test-secret-value", config.read_text(encoding="utf-8"))

    def test_build_argv_rejects_malformed_authorization_header_file(self) -> None:
        for value in ("", "test-secret-value", "Basic dGVzdA==", "Bearer has spaces"):
            with self.subTest(value=value), TemporaryDirectory() as tmp:
                root = Path(tmp)
                header_file = root / "backend-authorization.txt"
                header_file.write_text(value, encoding="utf-8")
                config = root / "task.json"
                config.write_text(
                    json.dumps(
                        {
                            "log_file": "C:/logs/mcp.log",
                            "server_args": ["--workspace", "D:/coding-tools-mcp"],
                            "auth_header_file": str(header_file),
                        }
                    ),
                    encoding="utf-8",
                )
                with self.assertRaises(ValueError):
                    build_argv(config)

    def test_config_hash_lock_rejects_concurrent_configuration_drift(self) -> None:
        with TemporaryDirectory() as tmp:
            config = Path(tmp) / "task.json"
            config.write_text(
                json.dumps({"log_file": "x", "server_args": ["--workspace", "D:/"]}),
                encoding="utf-8",
            )
            expected = sha256(config.read_bytes()).hexdigest()
            verify_config_hash(config, expected)

            config.write_text(
                json.dumps({"log_file": "x", "server_args": ["--workspace", "E:/"]}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "hash does not match"):
                verify_config_hash(config, expected)

    def test_verified_snapshot_is_the_same_snapshot_used_to_build_argv(self) -> None:
        with TemporaryDirectory() as tmp:
            config = Path(tmp) / "task.json"
            original = {
                "log_file": "x",
                "server_args": ["--workspace", "D:/original"],
                "require_config_sha256_pin": True,
            }
            config.write_text(json.dumps(original), encoding="utf-8")
            raw_bytes, raw = load_config_snapshot(config)
            expected = sha256(raw_bytes).hexdigest()

            config.write_text(
                json.dumps(
                    {
                        "log_file": "x",
                        "server_args": ["--workspace", "E:/drifted"],
                        "require_config_sha256_pin": True,
                    }
                ),
                encoding="utf-8",
            )

            verify_config_snapshot(raw_bytes, expected)
            argv = build_argv_from_config(raw)
            self.assertIn("D:/original", argv)
            self.assertNotIn("E:/drifted", argv)

    def test_required_windows_administrator_contract_fails_closed(self) -> None:
        with patch("scripts.run_local_mcp_http_task.os.name", "nt"):
            require_windows_administrator(True, is_administrator=lambda: True)
            with self.assertRaisesRegex(RuntimeError, "administrator token"):
                require_windows_administrator(True, is_administrator=lambda: False)

    def test_required_external_hash_pin_fails_closed_when_task_action_omits_it(self) -> None:
        require_config_hash_pin(True, "a" * 64)
        with self.assertRaisesRegex(RuntimeError, "SHA-256 pin"):
            require_config_hash_pin(True, None)


if __name__ == "__main__":
    unittest.main()
