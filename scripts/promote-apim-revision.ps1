param(
    [Parameter(Mandatory)] [string] $ResourceGroup,
    [Parameter(Mandatory)] [string] $ServiceName,
    [Parameter(Mandatory)] [string] $ApiId,
    [Parameter(Mandatory)] [string] $StableRevision,
    [Parameter(Mandatory)] [string] $CandidateRevision,
    [string] $ReleaseId = 'migration-current'
)

$ErrorActionPreference = 'Stop'
$serviceId = az apim show --resource-group $ResourceGroup --name $ServiceName --query id -o tsv
if ($LASTEXITCODE -ne 0 -or -not $serviceId) { throw 'Unable to resolve the APIM service.' }
$releaseUri = "$serviceId/apis/$ApiId/releases/$ReleaseId`?api-version=2024-05-01"
$currentBaseUrl = $env:APIM_OPENAI_BASE_URL
if (-not $currentBaseUrl) { throw 'Set APIM_OPENAI_BASE_URL before running the revision gate.' }
$candidateBaseUrl = $currentBaseUrl -replace '/openai(?=/|$)', "/openai;rev=$CandidateRevision"
if ($candidateBaseUrl -eq $currentBaseUrl) { throw 'APIM_OPENAI_BASE_URL must contain the /openai API path.' }

function Set-CurrentRevision {
    [CmdletBinding(SupportsShouldProcess)]
    param([string] $Revision, [string] $Notes)

    $apiRevisionId = "$serviceId/apis/$ApiId;rev=$Revision"
    $body = @{ properties = @{ apiId = $apiRevisionId; notes = $Notes } } | ConvertTo-Json -Compress
    if ($PSCmdlet.ShouldProcess($apiRevisionId, 'Set current APIM revision')) {
        az rest --method put --uri $releaseUri --body $body --headers 'Content-Type=application/json' --output none
        if ($LASTEXITCODE -ne 0) { throw "Unable to release APIM revision $Revision." }
    }
}

try {
    $env:APIM_OPENAI_BASE_URL = $candidateBaseUrl
    python "$PSScriptRoot\..\smoke_test.py" --target apim
    if ($LASTEXITCODE -ne 0) { throw 'Candidate revision smoke test failed before promotion.' }

    Set-CurrentRevision $CandidateRevision 'Automated OpenAI v1 promotion after synthetic canary'
    $env:APIM_OPENAI_BASE_URL = $currentBaseUrl
    python "$PSScriptRoot\..\smoke_test.py" --target apim
    if ($LASTEXITCODE -ne 0) { throw 'Post-promotion smoke test failed.' }
    python "$PSScriptRoot\..\load_test.py" --target apim --requests 10 --concurrency 2
    if ($LASTEXITCODE -ne 0) { throw 'Post-promotion bounded load test failed.' }
    Write-Output "APIM revision $CandidateRevision passed the canary gate and remains current."
}
catch {
    $env:APIM_OPENAI_BASE_URL = $currentBaseUrl
    Set-CurrentRevision $StableRevision 'Automatic rollback after failed OpenAI v1 canary gate'
    Write-Error "Candidate revision failed; APIM revision $StableRevision was restored. $($_.Exception.Message)"
    exit 1
}
finally {
    $env:APIM_OPENAI_BASE_URL = $currentBaseUrl
}
