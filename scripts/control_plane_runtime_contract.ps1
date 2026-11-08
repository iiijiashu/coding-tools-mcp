function Import-ControlPlaneRuntimeContract {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)] [string] $RepositoryRoot,
        [Parameter(Mandatory)] [string] $ContractPath,
        [Parameter(Mandatory)] [string] $PythonExecutable
    )

    $root = [IO.Path]::GetFullPath($RepositoryRoot)
    $contract = [IO.Path]::GetFullPath($ContractPath)
    $python = [IO.Path]::GetFullPath($PythonExecutable)
    if (-not (Test-Path -LiteralPath $contract -PathType Leaf)) {
        throw "Runtime contract is missing: $contract"
    }
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
        throw "Python runtime is missing: $python"
    }

    $loader = Join-Path $root 'coding_tools_mcp\control_plane_runtime_contract.py'
    if (-not (Test-Path -LiteralPath $loader -PathType Leaf)) {
        throw "Runtime contract loader is missing: $loader"
    }
    $previousErrorAction = $ErrorActionPreference
    $previousNativePreference = $PSNativeCommandUseErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $PSNativeCommandUseErrorActionPreference = $false
        $output = & $python $loader --contract $contract 2>&1
        $pythonExitCode = $LASTEXITCODE
    } finally {
        $PSNativeCommandUseErrorActionPreference = $previousNativePreference
        $ErrorActionPreference = $previousErrorAction
    }
    if ($pythonExitCode -ne 0) {
        throw "Runtime contract validation failed: $($output -join [Environment]::NewLine)"
    }
    try {
        return ($output -join [Environment]::NewLine) | ConvertFrom-Json
    } catch {
        throw "Runtime contract validator returned invalid JSON: $($_.Exception.Message)"
    }
}
