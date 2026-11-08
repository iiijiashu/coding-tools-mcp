from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time
import unittest
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
RECOVERY_SCRIPT = SCRIPTS / "control_plane_task_recovery.ps1"


class RecoveryDeploymentScriptTests(unittest.TestCase):
    @staticmethod
    def _powershell() -> str:
        executable = shutil.which("pwsh.exe") or shutil.which("powershell.exe")
        if executable is None:
            raise unittest.SkipTest("PowerShell is not available")
        return executable

    def test_journal_writer_uses_same_directory_flush_and_atomic_publish(self) -> None:
        text = RECOVERY_SCRIPT.read_text(encoding="utf-8-sig")
        self.assertIn("[IO.Path]::GetDirectoryName($targetPath)", text)
        self.assertIn("$stream.Flush($true)", text)
        self.assertIn(
            "[IO.File]::Replace($temporaryPath, $targetPath, $replacementBackupPath)",
            text,
        )
        self.assertIn("[IO.File]::Move($temporaryPath, $targetPath)", text)

    def test_atomic_journal_writer_publishes_valid_json_without_temp_residue(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            target = temporary / "journal.json"
            target.write_text('{"state":"stale"}', encoding="utf-8")
            harness = temporary / "write-journal.ps1"
            harness.write_text(
                "\n".join(
                    (
                        f". '{RECOVERY_SCRIPT.as_posix()}'",
                        "$payload = [ordered]@{ schema_version = 1; state = 'prepared' }",
                        f"Write-JsonFileAtomic -LiteralPath '{target.as_posix()}' -InputObject $payload -Depth 4",
                    )
                ),
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    self._powershell(),
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-File",
                    str(harness),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=20,
                check=False,
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertEqual(
                json.loads(target.read_text(encoding="utf-8"))["state"],
                "prepared",
                msg=f"stdout={result.stdout!r} stderr={result.stderr!r}",
            )
            self.assertEqual(list(temporary.glob(".journal.json.*.tmp")), [])
            self.assertEqual(list(temporary.glob(".journal.json.*.tmp.bak")), [])

    def test_deployment_mutex_rejects_an_overlapping_holder_within_bound(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            ready = temporary / "ready.txt"
            mutex_name = f"Global\\CodingToolsMcp.Test.{uuid.uuid4().hex}"
            holder_script = temporary / "holder.ps1"
            holder_script.write_text(
                "\n".join(
                    (
                        "$ErrorActionPreference = 'Stop'",
                        f". '{RECOVERY_SCRIPT.as_posix()}'",
                        f"$mutex = Enter-ControlPlaneDeploymentMutex -Name '{mutex_name}' -TimeoutSeconds 2",
                        "try {",
                        f"  [IO.File]::WriteAllText('{ready.as_posix()}', 'ready')",
                        "  Start-Sleep -Seconds 3",
                        "} finally { Exit-ControlPlaneDeploymentMutex -Mutex $mutex }",
                    )
                ),
                encoding="utf-8",
            )
            contender_script = temporary / "contender.ps1"
            contender_script.write_text(
                "\n".join(
                    (
                        "$ErrorActionPreference = 'Stop'",
                        f". '{RECOVERY_SCRIPT.as_posix()}'",
                        "try {",
                        f"  $mutex = Enter-ControlPlaneDeploymentMutex -Name '{mutex_name}' -TimeoutSeconds 0",
                        "  Exit-ControlPlaneDeploymentMutex -Mutex $mutex",
                        "  exit 0",
                        "} catch {",
                        "  Write-Output $_.Exception.Message",
                        "  exit 8",
                        "}",
                    )
                ),
                encoding="utf-8",
            )
            holder = subprocess.Popen(
                [
                    self._powershell(),
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-File",
                    str(holder_script),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            try:
                deadline = time.monotonic() + 10
                while not ready.exists() and holder.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue(ready.exists(), msg="mutex holder did not become ready")
                contender = subprocess.run(
                    [
                        self._powershell(),
                        "-NoLogo",
                        "-NoProfile",
                        "-NonInteractive",
                        "-File",
                        str(contender_script),
                    ],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=10,
                    check=False,
                )
                self.assertEqual(contender.returncode, 8, msg=contender.stderr)
                self.assertIn("did not finish within 0 seconds", contender.stdout)
            finally:
                holder.communicate(timeout=10)

    def test_invalid_journal_aborts_recovery_before_any_task_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            journal_root = temporary / "deployments"
            valid_directory = journal_root / "a-valid"
            invalid_directory = journal_root / "z-invalid"
            valid_directory.mkdir(parents=True)
            invalid_directory.mkdir(parents=True)
            backup = valid_directory / "TaskA.xml"
            backup.write_text("<Task />", encoding="utf-8")
            (valid_directory / "journal.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "state": "applying",
                        "changed_tasks": ["TaskA"],
                        "backups": {"TaskA": str(backup)},
                    }
                ),
                encoding="utf-8",
            )
            (invalid_directory / "journal.json").write_text(
                '{"schema_version":1,"state":"applying"', encoding="utf-8"
            )
            mutation_marker = temporary / "mutation.txt"
            harness = temporary / "recover.ps1"
            harness.write_text(
                "\n".join(
                    (
                        "$ErrorActionPreference = 'Stop'",
                        f". '{RECOVERY_SCRIPT.as_posix()}'",
                        "function Stop-ScheduledTaskBounded { param($Name, $TaskPath) "
                        f"[IO.File]::WriteAllText('{mutation_marker.as_posix()}', $Name) }}",
                        "function Register-ScheduledTask { param($TaskPath, $TaskName, $Xml, [switch]$Force) }",
                        "function Assert-ScheduledTaskBackupRestored { param($Name, $ExpectedXml, $TaskPath) }",
                        "try {",
                        f"  Recover-UnfinishedDeploymentJournals -JournalRoot '{journal_root.as_posix()}' "
                        "-JournalFileName 'journal.json'",
                        "  Write-Output '{\"ok\":true}'",
                        "} catch {",
                        "  [pscustomobject]@{ ok = $false; error = $_.Exception.Message } "
                        "| ConvertTo-Json -Compress",
                        "  exit 7",
                        "}",
                    )
                ),
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    self._powershell(),
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-File",
                    str(harness),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=20,
                check=False,
            )
            self.assertEqual(result.returncode, 7, msg=result.stderr)
            self.assertIn("truncated or invalid JSON", result.stdout)
            self.assertFalse(mutation_marker.exists())

    def test_rollback_fingerprint_covers_enabled_restart_and_trigger_semantics(self) -> None:
        text = RECOVERY_SCRIPT.read_text(encoding="utf-8-sig")
        for field in (
            "Enabled = NodeText '//t:Settings/t:Enabled'",
            "RestartOnFailureCount = NodeText '//t:Settings/t:RestartOnFailure/t:Count'",
            "RestartOnFailureInterval = NodeText '//t:Settings/t:RestartOnFailure/t:Interval'",
            "RegistrationTriggerXml = NodeXml '//t:RegistrationTrigger'",
            "LogonTriggerXml = NodeXml '//t:LogonTrigger'",
            "SessionTriggerXml = NodeXml '//t:SessionStateChangeTrigger'",
            "TimeTriggerXml = NodeXml '//t:TimeTrigger'",
        ):
            self.assertIn(field, text)

    def test_canonical_deployer_acquires_bounded_global_mutex_before_catalog(self) -> None:
        text = (SCRIPTS / "deploy_control_plane_supervisors.ps1").read_text(
            encoding="utf-8-sig"
        )
        acquisition = text.index("Enter-ControlPlaneDeploymentMutex")
        catalog = text.index("$catalogSnapshot = Get-LiveCatalogSnapshot")
        self.assertLess(acquisition, catalog)
        self.assertIn("Exit-ControlPlaneDeploymentMutex", text)
        common = RECOVERY_SCRIPT.read_text(encoding="utf-8-sig")
        self.assertIn("Global\\CodingToolsMcp.ControlPlaneDeployment.v1", common)
        self.assertIn("WaitOne([TimeSpan]::FromSeconds($TimeoutSeconds))", common)

    def test_canonical_deployer_requires_elevated_admin_before_mutex_and_catalog(self) -> None:
        text = (SCRIPTS / "deploy_control_plane_supervisors.ps1").read_text(
            encoding="utf-8-sig"
        )
        elevation = text.index(
            "IsInRole([System.Security.Principal.WindowsBuiltInRole]::Administrator)"
        )
        mutex = text.index("$deploymentMutex = Enter-ControlPlaneDeploymentMutex")
        catalog = text.index("$catalogSnapshot = Get-LiveCatalogSnapshot")
        self.assertLess(elevation, mutex)
        self.assertLess(mutex, catalog)
        self.assertIn("requires an elevated administrator token", text)

    def test_recovery_preflight_has_deterministic_newest_first_order(self) -> None:
        text = RECOVERY_SCRIPT.read_text(encoding="utf-8-sig")
        self.assertIn("@{ Expression = 'LastWriteTimeUtc'; Descending = $true }", text)
        self.assertIn("@{ Expression = 'FullName'; Descending = $true }", text)

    def test_supervisor_deploy_journals_before_register_and_removes_time_triggers(self) -> None:
        text = (SCRIPTS / "deploy_control_plane_supervisors.ps1").read_text(
            encoding="utf-8-sig"
        )
        marker = text.index("Write-Journal -State 'about_to_apply'")
        mutation = text.index(
            "Register-ScheduledTask -TaskPath $taskPath -TaskName $Spec.Name",
            marker,
        )
        self.assertLess(marker, mutation)
        self.assertIn("Assert-ApprovedTunnelContract", text)
        self.assertIn("Recover-UnfinishedDeploymentJournals", text)
        self.assertNotIn("New-TaskElement -Document $xml -Name 'TimeTrigger'", text)
        self.assertIn("Task must contain exactly one Exec action", text)
        self.assertIn("--loop-interval-seconds $($Spec.LoopIntervalSeconds)", text)

    def test_canonical_deployer_uses_the_validated_runtime_contract(self) -> None:
        text = (SCRIPTS / "deploy_control_plane_supervisors.ps1").read_text(
            encoding="utf-8-sig"
        )
        self.assertIn("Import-ControlPlaneRuntimeContract", text)
        self.assertIn("control-plane-runtime-contract-v1.json", text)
        self.assertIn("--catalog-snapshot-only", text)
        self.assertIn("$catalogSnapshot.tool_catalog.sha256", text)
        self.assertIn("$contract.tunnel.main_arguments_sha256", text)
        for duplicated_value in (
            "Coding Tools MCP HTTP Watchdog - Local D Drive",
            "OpenAI Secure MCP Tunnel Watchdog - Local D Drive Coding Tools",
            "Coding Tools MCP Control Plane Manager - Local D Drive",
            "e84baec972add898c3db64cf5e83e6098b4ecf57872fefdfd4e5611daa67a6e8",
            "e57538e1c619b731da65b26c48d50b566b6af9fa3a90ecb79497a4b44a13388d",
            "local_http_mcp_watchdog.py",
            "secure_tunnel_watchdog.py",
            "control_plane_transaction.py",
            "LoopIntervalSeconds = 15",
            "LoopIntervalSeconds = 60",
        ):
            self.assertNotIn(duplicated_value, text)

    def test_required_triggers_do_not_delete_or_reject_extra_recovery_triggers(self) -> None:
        text = (SCRIPTS / "deploy_control_plane_supervisors.ps1").read_text(
            encoding="utf-8-sig"
        )
        self.assertIn("$required -and $count -ne 1", text)
        self.assertNotIn("-not $required -and", text)

    def test_deployer_rejects_unknown_trigger_types_but_preserves_session_recovery(self) -> None:
        text = (SCRIPTS / "deploy_control_plane_supervisors.ps1").read_text(
            encoding="utf-8-sig"
        )
        self.assertIn("SessionStateChangeTrigger", text)
        self.assertIn("Unexpected trigger type", text)
        self.assertIn("$allowedTriggerTypes", text)

    def test_deployer_validates_exact_script_and_tunnel_executable_contract(self) -> None:
        text = (SCRIPTS / "deploy_control_plane_supervisors.ps1").read_text(
            encoding="utf-8-sig"
        )
        self.assertIn("Get-FirstCommandLineArgument", text)
        self.assertNotIn('-notlike "*$($Spec.Script)*"', text)
        self.assertIn("$contract.tunnel.executable_sha256", text)
        self.assertIn("Tunnel executable fingerprint", text)
        self.assertIn("--tunnel-client", text)
        self.assertIn("manager action arguments", text)

    def test_canonical_runtime_contract_bridge_uses_python_validator(self) -> None:
        text = (SCRIPTS / "control_plane_runtime_contract.ps1").read_text(
            encoding="utf-8-sig"
        )
        self.assertIn("coding_tools_mcp\\control_plane_runtime_contract.py", text)
        self.assertIn("--contract $contract", text)
        self.assertIn("ConvertFrom-Json", text)

    def test_rollbacks_share_bounded_stop_and_exact_post_verification(self) -> None:
        common = RECOVERY_SCRIPT.read_text(encoding="utf-8-sig")
        self.assertIn("Stop-ScheduledTaskBounded", common)
        self.assertIn("Assert-ScheduledTaskBackupRestored", common)
        self.assertIn("Rollback verification mismatch", common)
        self.assertIn("status = 'rollback_partial'", common)

    def test_deployment_failure_paths_verify_each_restored_task(self) -> None:
        text = (SCRIPTS / "deploy_control_plane_supervisors.ps1").read_text(
            encoding="utf-8-sig"
        )
        self.assertIn(
            "Assert-ScheduledTaskBackupRestored -Name $name",
            text,
            msg="canonical deployer can claim rollback without verifying the restored task",
        )


if __name__ == "__main__":
    unittest.main()
