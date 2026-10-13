from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from data_world.sdk import _build_claude_env, ClaudeRunner
from data_world.trajectory import CCRProxy
from task.runner import StreamingClaudeRunner


class ClaudeEnvironmentSecurityTests(unittest.TestCase):
    def test_only_target_model_credential_reaches_child(self) -> None:
        source = {
            "PATH": "/usr/bin",
            "ANTHROPIC_AUTH_TOKEN": "ambient-model-token",
            "JUDGE_API_KEY": "judge-only-key",
            "SWANLAB_API_KEY": "training-only-key",
            "AWS_ACCESS_KEY_ID": "cloud-access-id",
            "AWS_SECRET_ACCESS_KEY": "cloud-only-key",
            "SSH_AUTH_SOCK": "/tmp/ssh-agent.sock",
            "TOKENIZERS_PARALLELISM": "false",
            "no_proxy": "internal.example",
        }
        with mock.patch.dict(os.environ, source, clear=True):
            env = _build_claude_env("https://model.example", "target-model-token")

        self.assertEqual("target-model-token", env["ANTHROPIC_AUTH_TOKEN"])
        self.assertEqual("https://model.example", env["ANTHROPIC_BASE_URL"])
        self.assertEqual("false", env["TOKENIZERS_PARALLELISM"])
        self.assertNotIn("JUDGE_API_KEY", env)
        self.assertNotIn("SWANLAB_API_KEY", env)
        self.assertNotIn("AWS_ACCESS_KEY_ID", env)
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", env)
        self.assertNotIn("SSH_AUTH_SOCK", env)
        self.assertIn("127.0.0.1", env["no_proxy"])
        self.assertIn("localhost", env["NO_PROXY"])

    def test_non_streaming_token_is_not_put_in_process_arguments(self) -> None:
        runner = ClaudeRunner(
            executable="claude", model="model", endpoint="https://model.example",
            auth_token="target-model-token", timeout=10, max_turns=10,
            retries=0, verbose=False,
        )
        completed = SimpleNamespace(
            returncode=0,
            stdout=json.dumps({
                "is_error": False,
                "result": "ok",
                "stop_reason": "end_turn",
                "num_turns": 1,
            }),
            stderr="",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            with mock.patch("data_world.sdk.subprocess.run", return_value=completed) as run:
                ok, _, _ = runner.run("prompt", Path(temp_dir), [])

        self.assertTrue(ok)
        command = run.call_args.args[0]
        child_env = run.call_args.kwargs["env"]
        self.assertFalse(any("target-model-token" in arg for arg in command))
        settings = json.loads(command[command.index("--settings") + 1])
        self.assertEqual({"skipDangerousModePermissionPrompt": True}, settings)
        self.assertEqual("target-model-token", child_env["ANTHROPIC_AUTH_TOKEN"])

    def test_streaming_token_is_not_put_in_process_arguments(self) -> None:
        runner = StreamingClaudeRunner(
            executable="claude", model="model", endpoint="https://model.example",
            auth_token="target-model-token", timeout=10, max_turns=10,
            retries=0, verbose=False,
        )

        command = runner._build_command("prompt", Path("/tmp/work"), [], 10, False)

        self.assertFalse(any("target-model-token" in arg for arg in command))
        settings = json.loads(command[command.index("--settings") + 1])
        self.assertEqual({"skipDangerousModePermissionPrompt": True}, settings)
        self.assertEqual(
            "target-model-token", runner._child_env()["ANTHROPIC_AUTH_TOKEN"])


class CcrCredentialFileTests(unittest.TestCase):
    def test_ccr_process_does_not_inherit_unrelated_credentials(self) -> None:
        source = {
            "PATH": "/usr/bin",
            "JUDGE_API_KEY": "judge-only-key",
            "SWANLAB_API_KEY": "training-only-key",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            proxy = CCRProxy(
                "ccr", "https://model.example/v1", "target-model-token", "model",
                Path(temp_dir), 3456, False,
            )
            with mock.patch.dict(os.environ, source, clear=True):
                env = proxy._env()

        self.assertEqual("/usr/bin", env["PATH"])
        self.assertNotIn("JUDGE_API_KEY", env)
        self.assertNotIn("SWANLAB_API_KEY", env)

    def test_ccr_config_and_route_are_private(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            proxy = CCRProxy(
                "ccr", "https://model.example/v1", "target-model-token", "model",
                Path(temp_dir), 3456, False,
            )
            proxy._write_config()
            config_dir = Path(temp_dir) / ".claude-code-router"

            self.assertEqual(0o700, stat.S_IMODE(config_dir.stat().st_mode))
            self.assertEqual(
                0o600, stat.S_IMODE((config_dir / "config.json").stat().st_mode))
            self.assertEqual(
                0o600, stat.S_IMODE(proxy._route_script_path.stat().st_mode))


if __name__ == "__main__":
    unittest.main()
