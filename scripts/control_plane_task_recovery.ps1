function Write-JsonFileAtomic {
    param(
        [Parameter(Mandatory)] [string] $LiteralPath,
        [Parameter(Mandatory)] $InputObject,
        [int] $Depth = 8
    )
    $targetPath = [IO.Path]::GetFullPath($LiteralPath)
    $directory = [IO.Path]::GetDirectoryName($targetPath)
    if (-not (Test-Path -LiteralPath $directory -PathType Container)) {
        throw "Atomic JSON target directory does not exist: $directory"
    }
    $temporaryPath = Join-Path $directory (
        '.' + [IO.Path]::GetFileName($targetPath) + '.' + [guid]::NewGuid().ToString('N') + '.tmp'
    )
    $replacementBackupPath = $temporaryPath + '.bak'
    $stream = $null
    try {
        $json = $InputObject | ConvertTo-Json -Depth $Depth
        $bytes = [Text.UTF8Encoding]::new($false).GetBytes($json)
        $stream = [IO.FileStream]::new(
            $temporaryPath,
            [IO.FileMode]::CreateNew,
            [IO.FileAccess]::Write,
            [IO.FileShare]::None
        )
        $stream.Write($bytes, 0, $bytes.Length)
        $stream.Flush($true)
        $stream.Dispose()
        $stream = $null
        if ([IO.File]::Exists($targetPath)) {
            [IO.File]::Replace($temporaryPath, $targetPath, $replacementBackupPath)
        } else {
            [IO.File]::Move($temporaryPath, $targetPath)
        }
    } finally {
        if ($stream) { $stream.Dispose() }
        if ([IO.File]::Exists($temporaryPath)) {
            [IO.File]::Delete($temporaryPath)
        }
        if ([IO.File]::Exists($replacementBackupPath)) {
            [IO.File]::Delete($replacementBackupPath)
        }
    }
}

function Enter-ControlPlaneDeploymentMutex {
    param(
        [string] $Name = 'Global\CodingToolsMcp.ControlPlaneDeployment.v1',
        [int] $TimeoutSeconds = 30
    )
    $mutex = [Threading.Mutex]::new($false, $Name)
    $acquired = $false
    try {
        try {
            $acquired = $mutex.WaitOne([TimeSpan]::FromSeconds($TimeoutSeconds))
        } catch [Threading.AbandonedMutexException] {
            $acquired = $true
        }
        if (-not $acquired) {
            throw "Another control-plane deployment did not finish within $TimeoutSeconds seconds"
        }
        return $mutex
    } catch {
        $mutex.Dispose()
        throw
    }
}

function Exit-ControlPlaneDeploymentMutex {
    param([Parameter(Mandatory)] [Threading.Mutex] $Mutex)
    try {
        $Mutex.ReleaseMutex()
    } finally {
        $Mutex.Dispose()
    }
}

function Get-TaskContractFingerprint {
    param([Parameter(Mandatory)] [string] $XmlText)
    [xml] $xml = $XmlText
    $namespace = 'http://schemas.microsoft.com/windows/2004/02/mit/task'
    $ns = New-Object System.Xml.XmlNamespaceManager($xml.NameTable)
    $ns.AddNamespace('t', $namespace)
    $actionsNode = $xml.SelectSingleNode('//t:Actions', $ns)
    $actionNodes = @($actionsNode.ChildNodes | Where-Object {
        $_.NodeType -eq [System.Xml.XmlNodeType]::Element
    })
    function NodeText([string] $XPath) {
        $node = $xml.SelectSingleNode($XPath, $ns)
        if ($node) { return [string] $node.InnerText }
        return ''
    }
    function NodeXml([string] $XPath) {
        return (@($xml.SelectNodes($XPath, $ns) | ForEach-Object {
            [string] $_.OuterXml
        }) -join [char] 30)
    }
    return [ordered]@{
        UserId = NodeText '//t:Principal/t:UserId'
        LogonType = NodeText '//t:Principal/t:LogonType'
        RunLevel = NodeText '//t:Principal/t:RunLevel'
        MultipleInstances = NodeText '//t:Settings/t:MultipleInstancesPolicy'
        Enabled = NodeText '//t:Settings/t:Enabled'
        ExecutionTimeLimit = NodeText '//t:Settings/t:ExecutionTimeLimit'
        ActionCount = $actionNodes.Count
        ActionType = if ($actionNodes.Count -eq 1) { $actionNodes[0].LocalName } else { '' }
        Command = NodeText '//t:Actions/t:Exec/t:Command'
        Arguments = NodeText '//t:Actions/t:Exec/t:Arguments'
        WorkingDirectory = NodeText '//t:Actions/t:Exec/t:WorkingDirectory'
        RegistrationTriggers = @($xml.SelectNodes('//t:RegistrationTrigger', $ns)).Count
        LogonTriggers = @($xml.SelectNodes('//t:LogonTrigger', $ns)).Count
        SessionTriggers = @($xml.SelectNodes('//t:SessionStateChangeTrigger', $ns)).Count
        TimeTriggers = @($xml.SelectNodes('//t:TimeTrigger', $ns)).Count
        RestartOnFailure = @($xml.SelectNodes('//t:RestartOnFailure', $ns)).Count
        RestartOnFailureCount = NodeText '//t:Settings/t:RestartOnFailure/t:Count'
        RestartOnFailureInterval = NodeText '//t:Settings/t:RestartOnFailure/t:Interval'
        RegistrationTriggerXml = NodeXml '//t:RegistrationTrigger'
        LogonTriggerXml = NodeXml '//t:LogonTrigger'
        SessionTriggerXml = NodeXml '//t:SessionStateChangeTrigger'
        TimeTriggerXml = NodeXml '//t:TimeTrigger'
    }
}

function Stop-ScheduledTaskBounded {
    param(
        [Parameter(Mandatory)] [string] $Name,
        [string] $TaskPath = '\',
        [int] $TimeoutSeconds = 20
    )
    $task = Get-ScheduledTask -TaskPath $TaskPath -TaskName $Name
    if ([string] $task.State -ne 'Running') { return }
    Stop-ScheduledTask -TaskPath $TaskPath -TaskName $Name
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        $task = Get-ScheduledTask -TaskPath $TaskPath -TaskName $Name
        if ([string] $task.State -ne 'Running') { return }
        Start-Sleep -Milliseconds 250
    } while ([DateTime]::UtcNow -lt $deadline)
    throw "Task did not stop before rollback: $Name"
}

function Assert-ScheduledTaskBackupRestored {
    param(
        [Parameter(Mandatory)] [string] $Name,
        [Parameter(Mandatory)] [string] $ExpectedXml,
        [string] $TaskPath = '\'
    )
    $expected = Get-TaskContractFingerprint -XmlText $ExpectedXml
    $actual = Get-TaskContractFingerprint -XmlText (
        Export-ScheduledTask -TaskPath $TaskPath -TaskName $Name
    )
    foreach ($field in $expected.Keys) {
        if ([string] $actual[$field] -cne [string] $expected[$field]) {
            throw "Rollback verification mismatch for $Name field $field"
        }
    }
}

function Restore-ScheduledTaskSet {
    param(
        [Parameter(Mandatory)] [string] $BackupRoot,
        [Parameter(Mandatory)] [System.Collections.IDictionary] $Backups,
        [Parameter(Mandatory)] [string[]] $StopOrder,
        [Parameter(Mandatory)] [string[]] $RestoreOrder,
        [Parameter(Mandatory)] [string] $StatusLabel,
        [string] $TaskPath = '\',
        [string] $DoctorPython,
        [string] $DoctorScript
    )
    $restored = [System.Collections.Generic.List[string]]::new()
    try {
        $rootFull = [IO.Path]::GetFullPath($BackupRoot).TrimEnd('\') + '\'
        foreach ($name in $Backups.Keys) {
            $path = [IO.Path]::GetFullPath((Join-Path $BackupRoot ([string] $Backups[$name])))
            if (-not $path.StartsWith($rootFull, [StringComparison]::OrdinalIgnoreCase)) {
                throw "Rollback backup escapes the approved root: $name"
            }
            if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
                throw "Missing rollback backup: $path"
            }
        }
        foreach ($name in $StopOrder) {
            Stop-ScheduledTaskBounded -Name $name -TaskPath $TaskPath
        }
        foreach ($name in $RestoreOrder) {
            $path = Join-Path $BackupRoot ([string] $Backups[$name])
            $xml = [IO.File]::ReadAllText($path)
            Register-ScheduledTask -TaskPath $TaskPath -TaskName $name -Xml $xml -Force | Out-Null
            Assert-ScheduledTaskBackupRestored -Name $name -ExpectedXml $xml -TaskPath $TaskPath
            $restored.Add($name)
        }
        $doctorStatus = 'not_run'
        if ($DoctorPython -and $DoctorScript) {
            $doctorOutput = & $DoctorPython $DoctorScript 2>&1
            try {
                $doctorStatus = [string] (($doctorOutput | ConvertFrom-Json).status)
            } catch {
                $doctorStatus = 'unparseable'
            }
        }
        return [pscustomobject]@{
            ok = $true
            status = $StatusLabel
            restored_tasks = @($restored)
            doctor_status = $doctorStatus
            time = [DateTime]::UtcNow.ToString('o')
        }
    } catch {
        return [pscustomobject]@{
            ok = $false
            status = 'rollback_partial'
            restored_tasks = @($restored)
            error = $_.Exception.Message
            time = [DateTime]::UtcNow.ToString('o')
        }
    }
}

function Recover-UnfinishedDeploymentJournals {
    param(
        [Parameter(Mandatory)] [string] $JournalRoot,
        [Parameter(Mandatory)] [string] $JournalFileName,
        [string] $TaskPath = '\'
    )
    if (-not (Test-Path -LiteralPath $JournalRoot -PathType Container)) { return }
    $terminal = @('committed', 'rolled_back', 'recovered_rolled_back', 'recovered_no_changes')
    $journalFiles = @(Get-ChildItem -LiteralPath $JournalRoot -Recurse -File |
        Where-Object { $_.Name -eq $JournalFileName } |
        Sort-Object -Property `
            @{ Expression = 'LastWriteTimeUtc'; Descending = $true }, `
            @{ Expression = 'FullName'; Descending = $true })
    $journals = [System.Collections.Generic.List[object]]::new()
    foreach ($journalPath in $journalFiles) {
        try {
            $journal = [IO.File]::ReadAllText($journalPath.FullName) | ConvertFrom-Json -ErrorAction Stop
        } catch {
            throw "Deployment journal is truncated or invalid JSON: $($journalPath.FullName)"
        }
        if (
            $null -eq $journal -or
            $journal.schema_version -ne 1 -or
            -not $journal.PSObject.Properties['state']
        ) {
            throw "Deployment journal has an invalid schema: $($journalPath.FullName)"
        }
        $journals.Add([pscustomobject]@{ Path = $journalPath; Journal = $journal })
    }
    foreach ($entry in $journals) {
        $journalPath = $entry.Path
        $journal = $entry.Journal
        if ([string] $journal.state -in $terminal) { continue }
        $changed = @($journal.changed_tasks)
        if ($changed.Count -eq 0) {
            $journal.state = 'recovered_no_changes'
            Write-JsonFileAtomic -LiteralPath $journalPath.FullName -InputObject $journal -Depth 8
            continue
        }
        for ($index = $changed.Count - 1; $index -ge 0; $index--) {
            $name = [string] $changed[$index]
            $property = $journal.backups.PSObject.Properties[$name]
            if (-not $property) { throw "Unfinished deployment has no backup for $name" }
            $backupPath = [IO.Path]::GetFullPath([string] $property.Value)
            $journalDirectory = [IO.Path]::GetFullPath($journalPath.DirectoryName).TrimEnd('\') + '\'
            if (-not $backupPath.StartsWith($journalDirectory, [StringComparison]::OrdinalIgnoreCase)) {
                throw "Unfinished deployment backup escapes its journal directory: $name"
            }
            if (-not (Test-Path -LiteralPath $backupPath -PathType Leaf)) {
                throw "Unfinished deployment backup is missing: $name"
            }
            Stop-ScheduledTaskBounded -Name $name -TaskPath $TaskPath
            $xml = [IO.File]::ReadAllText($backupPath)
            Register-ScheduledTask -TaskPath $TaskPath -TaskName $name -Xml $xml -Force | Out-Null
            Assert-ScheduledTaskBackupRestored -Name $name -ExpectedXml $xml -TaskPath $TaskPath
        }
        $journal.state = 'recovered_rolled_back'
        $journal | Add-Member -NotePropertyName recovered_at -NotePropertyValue ([DateTime]::UtcNow.ToString('o')) -Force
        Write-JsonFileAtomic -LiteralPath $journalPath.FullName -InputObject $journal -Depth 8
    }
}
