<#
.SYNOPSIS
Removes the obsolete APIM chat operation after dual-mode migration validation.

.DESCRIPTION
Created because incremental ARM deployments do not delete operations removed from
Bicep. It verifies that the replacement POST /v1/chat/completions operation exists
and that the obsolete operation has the expected identity and route before offering
a high-impact, ShouldProcess-protected deletion.
#>
[CmdletBinding(SupportsShouldProcess, ConfirmImpact = 'High')]
param(
    [Parameter(Mandatory)] [string] $ResourceGroup,
    [Parameter(Mandatory)] [string] $ServiceName,
    [Parameter(Mandatory)] [string] $ApiId
)

$ErrorActionPreference = 'Stop'
$obsoleteOperationId = 'unified-chat-completions'
$obsoletePath = '/chat/completions'
$currentPath = '/v1/chat/completions'

$serviceId = az apim show --resource-group $ResourceGroup --name $ServiceName --query id -o tsv
if ($LASTEXITCODE -ne 0 -or -not $serviceId) { throw 'Unable to resolve the APIM service.' }

$operations = az apim api operation list `
    --resource-group $ResourceGroup `
    --service-name $ServiceName `
    --api-id $ApiId `
    --query '[].{name:name,method:method,urlTemplate:urlTemplate}' `
    -o json | ConvertFrom-Json
if ($LASTEXITCODE -ne 0) { throw 'Unable to list APIM operations.' }

$currentOperation = @($operations | Where-Object {
    $_.method -eq 'POST' -and $_.urlTemplate -eq $currentPath
})
if ($currentOperation.Count -ne 1) {
    throw "Expected exactly one POST $currentPath operation before cleanup."
}

$obsoleteOperation = @($operations | Where-Object { $_.name -eq $obsoleteOperationId })
if ($obsoleteOperation.Count -eq 0) {
    Write-Output "APIM operation $obsoleteOperationId is already absent."
    exit 0
}
if ($obsoleteOperation.Count -ne 1 -or
    $obsoleteOperation[0].method -ne 'POST' -or
    $obsoleteOperation[0].urlTemplate -ne $obsoletePath) {
    throw "Operation $obsoleteOperationId does not match the expected POST $obsoletePath route."
}

$operationUri = "$serviceId/apis/$ApiId/operations/$obsoleteOperationId`?api-version=2024-05-01"
if ($PSCmdlet.ShouldProcess("$ServiceName/$ApiId/$obsoleteOperationId", "Delete obsolete POST $obsoletePath APIM operation")) {
    az rest --method delete --uri $operationUri --output none
    if ($LASTEXITCODE -ne 0) { throw "Unable to delete APIM operation $obsoleteOperationId." }

    $remainingOperation = az apim api operation show `
        --resource-group $ResourceGroup `
        --service-name $ServiceName `
        --api-id $ApiId `
        --operation-id $obsoleteOperationId `
        --query name `
        -o tsv 2>$null
    if ($LASTEXITCODE -eq 0 -or $remainingOperation) {
        throw "APIM operation $obsoleteOperationId still exists after deletion."
    }

    Write-Output "Removed obsolete APIM operation $obsoleteOperationId."
}
