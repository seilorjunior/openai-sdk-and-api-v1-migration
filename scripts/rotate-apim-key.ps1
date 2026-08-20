<#
.SYNOPSIS
Rotates the APIM subscription key used by the protected GitHub environment.

.DESCRIPTION
Created to rotate APIM credentials without an outage or exposing key values. It
regenerates the selected key slot, updates the GitHub environment secret, verifies
the new key with a smoke test, and invalidates the old slot only after validation.
#>
param(
    [Parameter(Mandatory)] [string] $ResourceGroup,
    [Parameter(Mandatory)] [string] $ServiceName,
    [Parameter(Mandatory)] [string] $SubscriptionId,
    [Parameter(Mandatory)] [ValidateSet('primary', 'secondary')] [string] $NewKeySlot,
    [Parameter(Mandatory)] [string] $GitHubEnvironment,
    [string] $GitHubSecretName = 'APIM_SUBSCRIPTION_KEY'
)

$ErrorActionPreference = 'Stop'
$serviceId = az apim show --resource-group $ResourceGroup --name $ServiceName --query id -o tsv
if ($LASTEXITCODE -ne 0 -or -not $serviceId) { throw 'Unable to resolve the APIM service.' }

$regenerateUri = "$serviceId/subscriptions/$SubscriptionId/regenerate$($NewKeySlot.Substring(0,1).ToUpper())$($NewKeySlot.Substring(1))Key?api-version=2024-05-01"
az rest --method post --uri $regenerateUri --output none
if ($LASTEXITCODE -ne 0) { throw "Unable to regenerate the $NewKeySlot APIM key." }

$secretsUri = "$serviceId/subscriptions/$SubscriptionId/listSecrets?api-version=2024-05-01"
$newKey = az rest --method post --uri $secretsUri --query "$($NewKeySlot)Key" -o tsv
if ($LASTEXITCODE -ne 0 -or -not $newKey) { throw 'Unable to retrieve the regenerated APIM key.' }

$newKey | gh secret set $GitHubSecretName --env $GitHubEnvironment
if ($LASTEXITCODE -ne 0) { throw 'Unable to update the protected GitHub environment secret.' }

$env:APIM_SUBSCRIPTION_KEY = $newKey
try {
    python "$PSScriptRoot\..\smoke_test.py" --target apim
    if ($LASTEXITCODE -ne 0) { throw 'Smoke validation failed; the old APIM key remains valid.' }

    $oldKeySlot = if ($NewKeySlot -eq 'primary') { 'secondary' } else { 'primary' }
    $oldRegenerateUri = "$serviceId/subscriptions/$SubscriptionId/regenerate$($oldKeySlot.Substring(0,1).ToUpper())$($oldKeySlot.Substring(1))Key?api-version=2024-05-01"
    az rest --method post --uri $oldRegenerateUri --output none
    if ($LASTEXITCODE -ne 0) { throw "New key validated, but the old $oldKeySlot key could not be invalidated." }
    Write-Output "APIM key rotation completed using the $NewKeySlot slot. No key value was printed."
}
finally {
    Remove-Item Env:APIM_SUBSCRIPTION_KEY -ErrorAction SilentlyContinue
    $newKey = $null
}
