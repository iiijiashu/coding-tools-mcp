$ErrorActionPreference = 'Stop'

$root = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$backupRoot = Join-Path $root 'artifacts\control-plane-supervisor-deployments'
. (Join-Path $PSScriptRoot 'control_plane_task_recovery.ps1')
. (Join-Path $PSScriptRoot 'control_plane_runtime_contract.ps1')

$contractPath = Join-Path $root 'config\control-plane-runtime-contract-v1.json'
$doctorPython = Join-Path $root '.venv\Scripts\python.exe'
$doctorScript = Join-Path $root 'scripts\control_plane_doctor.py'
$contract = Import-ControlPlaneRuntimeContract `
    -RepositoryRoot $root -ContractPath $contractPath -PythonExecutable $doctorPython
$taskPath = [string] $contract.task_path
$namespace = 'http://schemas.microsoft.com/windows/2004/02/mit/task'
$pythonw = Join-Path $root ([string] $contract.pythonw_relative)
$approvedTunnelArgumentsSha256 = [string] $contract.tunnel.main_arguments_sha256
$approvedTunnelExecutableSha256 = [string] $contract.tunnel.executable_sha256
$tunnelTaskName = [string] $contract.tasks.tunnel.name
$controlPlaneStateRoot = Join-Path $env:USERPROFILE '.local\state\tunnel-client\control-plane\local-d-drive-coding-tools'
$approvedTunnelExecutable = $null
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [System.Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([System.Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Control-plane supervisor deployment requires an elevated administrator token'
}
$allowedUsers = @($identity.User.Value, $identity.Name, ($identity.Name -split '\\')[-1])
$deploymentId = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffZ') + '-' + [guid]::NewGuid().ToString('N')
$deploymentRoot = Join-Path $backupRoot "deployments\$deploymentId"
$journalPath = Join-Path $deploymentRoot 'journal.json'
$changed = [System.Collections.Generic.List[string]]::new()
$backups = @{}

function Get-LiveCatalogSnapshot {
    $invocation = Invoke-ControlPlaneDoctorBounded `
        -TimeoutMilliseconds 30000 -Arguments @('--catalog-snapshot-only')
    if ($invocation.TimedOut) {
        throw 'Authenticated live catalog snapshot timed out after 30 seconds'
    }
    if ($invocation.ExitCode -ne 0) {
        throw (
            'Authenticated live catalog snapshot failed: ' +
            $invocation.StdErr
        )
    }
    try {
        $snapshot = $invocation.StdOut | ConvertFrom-Json
    } catch {
        throw "Authenticated live catalog snapshot returned invalid JSON: $($_.Exception.Message)"
    }
    $sha256 = [string] $snapshot.tool_catalog.sha256
    if ($snapshot.status -ne 'READY' -or $sha256 -notmatch '^[0-9a-f]{64}$') {
        throw 'Authenticated live catalog snapshot did not satisfy the deployment contract'
    }
    return $snapshot
}

function Invoke-ControlPlaneDoctorBounded {
    param(
        [ValidateRange(1, 120000)]
        [int] $TimeoutMilliseconds,
        [string[]] $Arguments = @()
    )

    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $doctorPython
    $allArguments = @($doctorScript) + @($Arguments)
    if (@($allArguments | Where-Object { $_ -match '"' }).Count -ne 0) {
        throw 'Control-plane doctor arguments may not contain double quotes'
    }
    $startInfo.Arguments = ($allArguments | ForEach-Object { '"' + $_ + '"' }) -join ' '
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $process = [System.Diagnostics.Process]::new()
    $process.StartInfo = $startInfo
    try {
        if (-not $process.Start()) {
            throw 'Control-plane doctor process did not start'
        }
        $stdoutTask = $process.StandardOutput.ReadToEndAsync()
        $stderrTask = $process.StandardError.ReadToEndAsync()
        $timedOut = -not $process.WaitForExit($TimeoutMilliseconds)
        if ($timedOut) {
            try {
                $process.Kill($true)
            } catch {
                if (-not $process.HasExited) {
                    $taskkillPath = Join-Path $env:SystemRoot 'System32\taskkill.exe'
                    $taskkillOutput = & $taskkillPath /PID $process.Id /T /F 2>&1
                    if ($LASTEXITCODE -ne 0 -and -not $process.HasExited) {
                        throw (
                            "Timed-out control-plane doctor tree could not be terminated: " +
                            ($taskkillOutput -join [Environment]::NewLine)
                        )
                    }
                }
            }
            if (-not $process.WaitForExit(5000)) {
                throw 'Timed-out control-plane doctor tree did not terminate within 5 seconds'
            }
        }
        $stdout = $stdoutTask.GetAwaiter().GetResult()
        $stderr = $stderrTask.GetAwaiter().GetResult()
        return [pscustomobject]@{
            TimedOut = $timedOut
            ExitCode = $(if ($timedOut) { $null } else { $process.ExitCode })
            StdOut = $stdout
            StdErr = $stderr
        }
    } finally {
        $process.Dispose()
    }
}

function Wait-ControlPlaneReady {
    $deadline = [DateTime]::UtcNow.AddSeconds(120)
    $lastDoctor = $null
    $lastParseError = $null
    do {
        $remainingMilliseconds = [int] [Math]::Max(
            1,
            [Math]::Ceiling(($deadline - [DateTime]::UtcNow).TotalMilliseconds)
        )
        $doctorInvocation = Invoke-ControlPlaneDoctorBounded `
            -TimeoutMilliseconds ([Math]::Min(30000, $remainingMilliseconds))
        $currentDoctor = $null
        if ($doctorInvocation.TimedOut) {
            $doctorExit = $null
            $lastParseError = 'Timeout'
        } else {
            $doctorExit = $doctorInvocation.ExitCode
            try {
                $currentDoctor = $doctorInvocation.StdOut | ConvertFrom-Json
                $lastDoctor = $currentDoctor
                $lastParseError = $null
            } catch {
                $lastParseError = $_.Exception.GetType().Name
            }
        }
        if ($doctorExit -eq 0 -and $currentDoctor -and $currentDoctor.status -eq 'READY') {
            return $currentDoctor
        }
        if ([DateTime]::UtcNow -lt $deadline) {
            Start-Sleep -Seconds 2
        }
    } while ([DateTime]::UtcNow -lt $deadline)

    $status = if ($lastDoctor) { [string] $lastDoctor.status } else { 'UNAVAILABLE' }
    $leaseReason = if ($lastDoctor -and $lastDoctor.control_plane) {
        [string] $lastDoctor.control_plane.lease_reason
    } else {
        'unknown'
    }
    $violations = if ($lastDoctor -and $lastDoctor.task_contract) {
        @($lastDoctor.task_contract.violations) -join ','
    } else {
        ''
    }
    $tunnelOk = if ($lastDoctor -and $lastDoctor.tunnel) {
        [string] $lastDoctor.tunnel.ok
    } else {
        'unknown'
    }
    throw (
        "Doctor did not become READY within 120 seconds: status=$status; " +
        "control_plane=$leaseReason; task_contract=$violations; " +
        "tunnel_ok=$tunnelOk; parse_error=$lastParseError"
    )
}

$deploymentMutex = Enter-ControlPlaneDeploymentMutex
$deploymentExitCode = 0
try {
    $catalogSnapshot = Get-LiveCatalogSnapshot
    $catalogSha256 = [string] $catalogSnapshot.tool_catalog.sha256

$specs = foreach ($key in @('http_watchdog', 'tunnel_watchdog', 'manager')) {
    $task = $contract.tasks.$key
    if (-not $task) { throw "Runtime contract is missing supervisor task: $key" }
    [pscustomobject]@{
        Key = $key
        Name = [string] $task.name
        RunLevel = if ([string] $task.run_level -eq 'Highest') { 'HighestAvailable' } else { 'Limited' }
        ObservedRunLevel = [string] $task.run_level
        Script = Join-Path $root ([string] $task.script_relative)
        MustBeRunning = [bool] $task.must_be_running
        RequiredTriggers = @($task.required_triggers)
        RestartOnFailure = [bool] $task.restart_on_failure
        AllowTimeTrigger = [bool] $task.allow_time_trigger
        LoopIntervalSeconds = [int] $task.loop_interval_seconds
        CatalogFingerprint = [bool] $task.pin_tool_catalog
        MainArgumentsSha256 = if ([bool] $task.pin_main_arguments) {
            $approvedTunnelArgumentsSha256
        } else {
            $null
        }
    }
}

function New-TaskElement {
    param([xml] $Document, [string] $Name, [AllowNull()] [string] $Value)
    $element = $Document.CreateElement($Name, $namespace)
    if ($null -ne $Value) { $element.InnerText = $Value }
    return $element
}

function Get-FirstCommandLineArgument {
    param([string] $Arguments)
    $match = [regex]::Match($Arguments, '^\s*(?:"([^"]+)"|(\S+))')
    if (-not $match.Success) { return $null }
    if ($match.Groups[1].Success) { return $match.Groups[1].Value }
    return $match.Groups[2].Value
}

function Get-SingleOptionValue {
    param([string] $Arguments, [string] $Option)
    $escaped = [regex]::Escape($Option)
    $matches = [regex]::Matches(
        $Arguments,
        "(?<!\S)$escaped(?:\s+|=)(?:`"([^`"]+)`"|(\S+))",
        [Text.RegularExpressions.RegexOptions]::IgnoreCase
    )
    if ($matches.Count -ne 1) { return $null }
    if ($matches[0].Groups[1].Success) { return $matches[0].Groups[1].Value }
    return $matches[0].Groups[2].Value
}

function Assert-SupervisorActionArguments {
    param(
        [string] $Arguments,
        [pscustomobject] $Spec,
        [switch] $RequireRuntimePins
    )
    $first = Get-FirstCommandLineArgument -Arguments $Arguments
    if (-not $first -or [IO.Path]::GetFullPath($first) -ne [IO.Path]::GetFullPath($Spec.Script)) {
        throw "Script mismatch: $($Spec.Name)"
    }
    if ($Spec.Key -eq 'tunnel_watchdog') {
        $tunnelClient = Get-SingleOptionValue -Arguments $Arguments -Option '--tunnel-client'
        if (
            -not $tunnelClient -or -not $approvedTunnelExecutable -or
            [IO.Path]::GetFullPath($tunnelClient) -ne [IO.Path]::GetFullPath($approvedTunnelExecutable)
        ) {
            throw "Tunnel watchdog client mismatch: $($Spec.Name)"
        }
        $tunnelClientSha256 = Get-SingleOptionValue `
            -Arguments $Arguments -Option '--tunnel-client-sha256'
        if (
            ($tunnelClientSha256 -and $tunnelClientSha256 -cne $approvedTunnelExecutableSha256) -or
            ($RequireRuntimePins -and -not $tunnelClientSha256)
        ) {
            throw "Tunnel watchdog client fingerprint mismatch: $($Spec.Name)"
        }
        if ((Get-SingleOptionValue -Arguments $Arguments -Option '--main-task-name') -cne $tunnelTaskName) {
            throw "Tunnel watchdog main task mismatch: $($Spec.Name)"
        }
        if ((Get-SingleOptionValue -Arguments $Arguments -Option '--main-task-path') -cne $taskPath) {
            throw "Tunnel watchdog main task path mismatch: $($Spec.Name)"
        }
    }
    if ($Spec.Key -eq 'manager') {
        $expected = "$($Spec.Script) execute --state-root $controlPlaneStateRoot --loop-interval-seconds $($Spec.LoopIntervalSeconds)"
        if ($Arguments.Trim() -cne $expected) {
            throw "Control-plane manager action arguments mismatch: $($Spec.Name)"
        }
    }
}

function Write-Journal {
    param([string] $State)
    $journal = [ordered]@{
        schema_version = 1
        deployment_id = $deploymentId
        state = $State
        changed_tasks = @($changed)
        backups = $backups
        time = [DateTime]::UtcNow.ToString('o')
    }
    Write-JsonFileAtomic -LiteralPath $journalPath -InputObject $journal -Depth 5
}

function Stop-TaskBounded {
    param([string] $Name)
    $task = Get-ScheduledTask -TaskPath $taskPath -TaskName $Name
    if ([string] $task.State -ne 'Running') { return }
    Stop-ScheduledTask -TaskPath $taskPath -TaskName $Name
    $deadline = [DateTime]::UtcNow.AddSeconds(20)
    do {
        $task = Get-ScheduledTask -TaskPath $taskPath -TaskName $Name
        if ([string] $task.State -ne 'Running') { return }
        Start-Sleep -Milliseconds 250
    } while ([DateTime]::UtcNow -lt $deadline)
    throw "Task did not stop: $Name"
}

function Assert-ApprovedTunnelContract {
    [xml] $xml = Export-ScheduledTask -TaskPath $taskPath -TaskName $tunnelTaskName
    $ns = New-Object System.Xml.XmlNamespaceManager($xml.NameTable)
    $ns.AddNamespace('t', $namespace)
    $actionsNode = $xml.SelectSingleNode('//t:Actions', $ns)
    $actionNodes = @($actionsNode.ChildNodes | Where-Object {
        $_.NodeType -eq [System.Xml.XmlNodeType]::Element
    })
    if ($actionNodes.Count -ne 1 -or $actionNodes[0].LocalName -ne 'Exec') {
        throw 'Tunnel task must contain exactly one Exec action before supervisor deployment'
    }
    $executable = [string] $xml.SelectSingleNode('//t:Actions/t:Exec/t:Command', $ns).InnerText
    if (-not [IO.File]::Exists($executable)) {
        throw 'Tunnel executable is unavailable before supervisor deployment'
    }
    $actualExecutableSha256 = (Get-FileHash -LiteralPath $executable -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualExecutableSha256 -cne $approvedTunnelExecutableSha256) {
        throw 'Tunnel executable fingerprint does not match the approved canonical contract'
    }
    $arguments = [string] $xml.SelectSingleNode('//t:Actions/t:Exec/t:Arguments', $ns).InnerText
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $actual = ([BitConverter]::ToString(
            $sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($arguments))
        )).Replace('-', '').ToLowerInvariant()
    } finally {
        $sha.Dispose()
    }
    if ($actual -cne $approvedTunnelArgumentsSha256) {
        throw 'Tunnel task arguments do not match the approved canonical contract'
    }
    return [pscustomobject]@{ Executable = $executable }
}

function Assert-TriggerContract {
    param([xml] $Xml, $NamespaceManager, [pscustomobject] $Spec)
    $triggerPaths = [ordered]@{
        registration = '//t:RegistrationTrigger'
        logon = '//t:LogonTrigger'
        session = '//t:SessionStateChangeTrigger'
    }
    foreach ($entry in $triggerPaths.GetEnumerator()) {
        $count = @($Xml.SelectNodes($entry.Value, $NamespaceManager)).Count
        $required = $entry.Key -in $Spec.RequiredTriggers
        if ($required -and $count -ne 1) {
            throw "Trigger contract mismatch for $($Spec.Name): $($entry.Key)"
        }
    }
    $allowedTriggerTypes = @(
        'RegistrationTrigger',
        'LogonTrigger',
        'SessionStateChangeTrigger',
        'TimeTrigger'
    )
    $triggersNode = $Xml.SelectSingleNode('//t:Triggers', $NamespaceManager)
    foreach ($trigger in @($triggersNode.ChildNodes | Where-Object {
        $_.NodeType -eq [System.Xml.XmlNodeType]::Element
    })) {
        if ($trigger.LocalName -notin $allowedTriggerTypes) {
            throw "Unexpected trigger type for $($Spec.Name): $($trigger.LocalName)"
        }
    }
}

function Update-Supervisor {
    param([pscustomobject] $Spec)
    $raw = Export-ScheduledTask -TaskPath $taskPath -TaskName $Spec.Name
    [xml] $xml = $raw
    $ns = New-Object System.Xml.XmlNamespaceManager($xml.NameTable)
    $ns.AddNamespace('t', $namespace)
    $principal = $xml.SelectSingleNode('//t:Principal', $ns)
    $user = [string] $principal.SelectSingleNode('t:UserId', $ns).InnerText
    if ($user -notin $allowedUsers) { throw "Principal mismatch: $($Spec.Name)" }
    $logon = [string] $principal.SelectSingleNode('t:LogonType', $ns).InnerText
    if ($logon -ne 'InteractiveToken') { throw "Logon type mismatch: $($Spec.Name)" }
    $runLevel = $principal.SelectSingleNode('t:RunLevel', $ns)
    if ($Spec.RunLevel -eq 'HighestAvailable') {
        if (-not $runLevel) {
            $runLevel = New-TaskElement -Document $xml -Name 'RunLevel' -Value 'HighestAvailable'
            $null = $principal.AppendChild($runLevel)
        } else {
            $runLevel.InnerText = 'HighestAvailable'
        }
    } elseif ($runLevel) {
        $null = $principal.RemoveChild($runLevel)
    }

    $settings = $xml.SelectSingleNode('//t:Settings', $ns)
    $multiple = $settings.SelectSingleNode('t:MultipleInstancesPolicy', $ns)
    if (-not $multiple) {
        $multiple = New-TaskElement -Document $xml -Name 'MultipleInstancesPolicy' -Value 'IgnoreNew'
        $null = $settings.PrependChild($multiple)
    } else {
        $multiple.InnerText = 'IgnoreNew'
    }
    $oldRestart = $settings.SelectSingleNode('t:RestartOnFailure', $ns)
    if ($oldRestart) { $null = $settings.RemoveChild($oldRestart) }
    if ($Spec.RestartOnFailure) {
        $restart = New-TaskElement -Document $xml -Name 'RestartOnFailure' -Value $null
        $null = $restart.AppendChild((New-TaskElement -Document $xml -Name 'Count' -Value '3'))
        $null = $restart.AppendChild((New-TaskElement -Document $xml -Name 'Interval' -Value 'PT1M'))
        $idle = $settings.SelectSingleNode('t:IdleSettings', $ns)
        if ($idle) {
            $null = $settings.InsertBefore($restart, $idle)
        } else {
            $null = $settings.AppendChild($restart)
        }
    }
    $executionLimit = $settings.SelectSingleNode('t:ExecutionTimeLimit', $ns)
    if (-not $executionLimit) { throw "ExecutionTimeLimit missing: $($Spec.Name)" }
    $executionLimit.InnerText = 'PT0S'

    $triggers = $xml.SelectSingleNode('//t:Triggers', $ns)
    Assert-TriggerContract -Xml $xml -NamespaceManager $ns -Spec $Spec
    if (-not $Spec.AllowTimeTrigger) {
        foreach ($oldTime in @($xml.SelectNodes('//t:TimeTrigger', $ns))) {
            $null = $triggers.RemoveChild($oldTime)
        }
    }

    $actionsNode = $xml.SelectSingleNode('//t:Actions', $ns)
    $actionNodes = @($actionsNode.ChildNodes | Where-Object {
        $_.NodeType -eq [System.Xml.XmlNodeType]::Element
    })
    if ($actionNodes.Count -ne 1 -or $actionNodes[0].LocalName -ne 'Exec') {
        throw "Task must contain exactly one Exec action: $($Spec.Name)"
    }
    $action = $xml.SelectSingleNode('//t:Actions/t:Exec', $ns)
    $command = [string] $action.SelectSingleNode('t:Command', $ns).InnerText
    if ([IO.Path]::GetFullPath($command) -ne [IO.Path]::GetFullPath($pythonw)) {
        throw "Executable mismatch: $($Spec.Name)"
    }
    $arguments = $action.SelectSingleNode('t:Arguments', $ns)
    if (-not $arguments) { throw "Arguments missing: $($Spec.Name)" }
    Assert-SupervisorActionArguments -Arguments $arguments.InnerText -Spec $Spec
    $loopPattern = '(?<!\S)--loop-interval-seconds(?:\s+|=)(?:"[^"]*"|\S+)'
    $arguments.InnerText = [regex]::Replace($arguments.InnerText, $loopPattern, '').Trim()
    if ($Spec.LoopIntervalSeconds -gt 0) {
        $arguments.InnerText += " --loop-interval-seconds $($Spec.LoopIntervalSeconds)"
    }
    $catalogPattern = '(?<!\S)--expected-tool-catalog-sha256(?:\s+|=)(?:"[^"]*"|\S+)'
    $arguments.InnerText = [regex]::Replace($arguments.InnerText, $catalogPattern, '').Trim()
    if ($Spec.CatalogFingerprint) {
        $arguments.InnerText += " --expected-tool-catalog-sha256 $catalogSha256"
    }
    $hashPattern = '(?<!\S)--main-task-arguments-sha256(?:\s+|=)(?:"[^"]*"|\S+)'
    $arguments.InnerText = [regex]::Replace($arguments.InnerText, $hashPattern, '').Trim()
    if ($Spec.MainArgumentsSha256) {
        $arguments.InnerText += " --main-task-arguments-sha256 $($Spec.MainArgumentsSha256)"
    }
    $clientHashPattern = '(?<!\S)--tunnel-client-sha256(?:\s+|=)(?:"[^"]*"|\S+)'
    $arguments.InnerText = [regex]::Replace($arguments.InnerText, $clientHashPattern, '').Trim()
    if ($Spec.Key -eq 'tunnel_watchdog') {
        $arguments.InnerText += " --tunnel-client-sha256 $approvedTunnelExecutableSha256"
    }

    if (-not $changed.Contains($Spec.Name)) { $changed.Add($Spec.Name) }
    Write-Journal -State 'about_to_apply'
    Stop-TaskBounded -Name $Spec.Name
    Register-ScheduledTask -TaskPath $taskPath -TaskName $Spec.Name -Xml $xml.OuterXml -Force | Out-Null
    Write-Journal -State 'applying'

    $deadline = [DateTime]::UtcNow.AddSeconds(25)
    do {
        $applied = Get-ScheduledTask -TaskPath $taskPath -TaskName $Spec.Name
        $state = [string] $applied.State
        if ($state -eq 'Running' -or (-not $Spec.MustBeRunning -and $state -eq 'Ready')) { break }
        Start-Sleep -Milliseconds 500
    } while ([DateTime]::UtcNow -lt $deadline)
    if ($state -ne 'Running' -and ($Spec.MustBeRunning -or $state -ne 'Ready')) {
        throw "Supervisor did not reach an approved state: $($Spec.Name)"
    }
    [xml] $appliedXml = Export-ScheduledTask -TaskPath $taskPath -TaskName $Spec.Name
    $appliedNs = New-Object System.Xml.XmlNamespaceManager($appliedXml.NameTable)
    $appliedNs.AddNamespace('t', $namespace)
    Assert-TriggerContract -Xml $appliedXml -NamespaceManager $appliedNs -Spec $Spec
    $appliedActionsNode = $appliedXml.SelectSingleNode('//t:Actions', $appliedNs)
    $appliedActionNodes = @($appliedActionsNode.ChildNodes | Where-Object {
        $_.NodeType -eq [System.Xml.XmlNodeType]::Element
    })
    $restartCount = @($appliedXml.SelectNodes('//t:RestartOnFailure', $appliedNs)).Count
    if (
        [string] $applied.Settings.MultipleInstances -ne 'IgnoreNew' -or
        [string] $applied.Principal.RunLevel -ne $Spec.ObservedRunLevel -or
        (-not $Spec.AllowTimeTrigger -and @($appliedXml.SelectNodes('//t:TimeTrigger', $appliedNs)).Count -ne 0) -or
        ($Spec.RestartOnFailure -and $restartCount -ne 1) -or
        (-not $Spec.RestartOnFailure -and $restartCount -ne 0) -or
        $appliedActionNodes.Count -ne 1 -or
        $appliedActionNodes[0].LocalName -ne 'Exec'
    ) {
        throw "Supervisor contract verification failed: $($Spec.Name)"
    }
    $appliedArguments = [string] $applied.Actions[0].Arguments
    Assert-SupervisorActionArguments `
        -Arguments $appliedArguments -Spec $Spec -RequireRuntimePins
    if ($Spec.LoopIntervalSeconds -gt 0 -and $appliedArguments -notmatch "(?<!\S)--loop-interval-seconds(?:\s+|=)$($Spec.LoopIntervalSeconds)(?:\s|$)") {
        throw "Supervisor loop interval verification failed: $($Spec.Name)"
    }
    if ($Spec.LoopIntervalSeconds -eq 0 -and $appliedArguments -match '(?<!\S)--loop-interval-seconds(?:\s|=)') {
        throw "Unexpected supervisor loop interval: $($Spec.Name)"
    }
    if ($Spec.CatalogFingerprint -and $appliedArguments -notmatch "(?<!\S)--expected-tool-catalog-sha256(?:\s+|=)$catalogSha256(?:\s|$)") {
        throw "Supervisor catalog fingerprint verification failed: $($Spec.Name)"
    }
    if (-not $Spec.CatalogFingerprint -and $appliedArguments -match '(?<!\S)--expected-tool-catalog-sha256(?:\s|=)') {
        throw "Unexpected supervisor catalog fingerprint: $($Spec.Name)"
    }
    if ($Spec.MainArgumentsSha256 -and $appliedArguments -notmatch "(?<!\S)--main-task-arguments-sha256(?:\s+|=)$($Spec.MainArgumentsSha256)(?:\s|$)") {
        throw "Supervisor main argument fingerprint verification failed: $($Spec.Name)"
    }
    if (-not $Spec.MainArgumentsSha256 -and $appliedArguments -match '(?<!\S)--main-task-arguments-sha256(?:\s|=)') {
        throw "Unexpected supervisor main argument fingerprint: $($Spec.Name)"
    }
}

try {
    $null = New-Item -ItemType Directory -Path $deploymentRoot -Force
    Recover-UnfinishedDeploymentJournals `
        -JournalRoot (Join-Path $backupRoot 'deployments') `
        -JournalFileName 'journal.json' -TaskPath $taskPath
    foreach ($spec in $specs) {
        $backup = Join-Path $deploymentRoot (($spec.Name -replace '[^A-Za-z0-9._-]', '_') + '.xml')
        [IO.File]::WriteAllText(
            $backup,
            (Export-ScheduledTask -TaskPath $taskPath -TaskName $spec.Name),
            [Text.UTF8Encoding]::new($false)
        )
        $backups[$spec.Name] = $backup
    }
    Write-Journal -State 'prepared'
    $approvedTunnel = Assert-ApprovedTunnelContract
    $approvedTunnelExecutable = [string] $approvedTunnel.Executable
    foreach ($spec in $specs) { Update-Supervisor -Spec $spec }
    $doctor = Wait-ControlPlaneReady
    Write-Journal -State 'committed'
    [pscustomobject]@{
        status = 'committed'
        deployment_id = $deploymentId
        changed_tasks = @($changed)
        doctor_status = $doctor.status
        time = [DateTime]::UtcNow.ToString('o')
    } | ConvertTo-Json -Compress
} catch {
    $failure = $_
    $rollbackErrors = [System.Collections.Generic.List[string]]::new()
    for ($index = $changed.Count - 1; $index -ge 0; $index--) {
        $name = $changed[$index]
        try {
            Stop-TaskBounded -Name $name
            $xml = [IO.File]::ReadAllText([string] $backups[$name])
            Register-ScheduledTask -TaskPath $taskPath -TaskName $name -Xml $xml -Force | Out-Null
            Assert-ScheduledTaskBackupRestored -Name $name -ExpectedXml $xml -TaskPath $taskPath
        } catch {
            $rollbackErrors.Add("$name`: $($_.Exception.Message)")
        }
    }
    Write-Journal -State $(if ($rollbackErrors.Count -eq 0) { 'rolled_back' } else { 'rollback_partial' })
    [pscustomobject]@{
        status = 'failed'
        error = $failure.Exception.Message
        rollback_errors = @($rollbackErrors)
        deployment_id = $deploymentId
        time = [DateTime]::UtcNow.ToString('o')
    } | ConvertTo-Json -Compress
    $deploymentExitCode = 1
}
} finally {
    Exit-ControlPlaneDeploymentMutex -Mutex $deploymentMutex
}
if ($deploymentExitCode -ne 0) {
    exit $deploymentExitCode
}
