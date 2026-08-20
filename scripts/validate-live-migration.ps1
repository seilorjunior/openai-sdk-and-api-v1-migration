param(
    [string] $ResourceGroup,
    [string] $ServiceName,
    [string] $ApiId,
    [string] $ApimSubscriptionId,
    [string] $ApplicationInsightsName,
    [string] $SubscriptionId,
    [string] $PythonExecutable = 'python',
    [switch] $SkipTelemetryCheck
)

$ErrorActionPreference = 'Stop'
$pythonCommand = $PythonExecutable

function Get-AzdValue([string] $Name) {
    $value = azd env get-value $Name
    if ($LASTEXITCODE -ne 0 -or -not $value) {
        throw "Unable to resolve azd value '$Name'."
    }
    return $value.Trim().Trim('"')
}

function Invoke-Checked([string] $Description, [scriptblock] $Command) {
    Write-Output "`n==> $Description"
    & $Command
    if ($LASTEXITCODE -ne 0) {
        throw "$Description failed with exit code $LASTEXITCODE."
    }
}

$environmentName = Get-AzdValue 'AZURE_ENV_NAME'
if (-not $ResourceGroup) { $ResourceGroup = "rg-$environmentName" }
if (-not $ServiceName) { $ServiceName = Get-AzdValue 'APIM_SERVICE_NAME' }
if (-not $ApiId) { $ApiId = Get-AzdValue 'APIM_API_ID' }
if (-not $ApimSubscriptionId) { $ApimSubscriptionId = Get-AzdValue 'APIM_SUBSCRIPTION_ID' }
if (-not $ApplicationInsightsName) { $ApplicationInsightsName = Get-AzdValue 'APPLICATION_INSIGHTS_NAME' }
if (-not $SubscriptionId) { $SubscriptionId = Get-AzdValue 'AZURE_SUBSCRIPTION_ID' }

Invoke-Checked 'Checking Azure CLI authentication' {
    az account show `
        --subscription $SubscriptionId `
        --query '{subscription:name, tenant:tenantId}' `
        --output table `
        --only-show-errors
}

$env:AZURE_OPENAI_BASE_URL = Get-AzdValue 'AZURE_OPENAI_BASE_URL'
$env:AZURE_OPENAI_DEPLOYMENT = Get-AzdValue 'AZURE_OPENAI_DEPLOYMENT'
$env:APIM_OPENAI_BASE_URL = Get-AzdValue 'APIM_OPENAI_BASE_URL'

$serviceId = az apim show `
    --subscription $SubscriptionId `
    --resource-group $ResourceGroup `
    --name $ServiceName `
    --query id `
    --output tsv `
    --only-show-errors
if ($LASTEXITCODE -ne 0 -or -not $serviceId) { throw 'Unable to resolve the APIM service.' }

$secretsUri = "$serviceId/subscriptions/$ApimSubscriptionId/listSecrets?api-version=2024-05-01"
$apimKey = az rest `
    --method post `
    --uri $secretsUri `
    --query primaryKey `
    --output tsv `
    --only-show-errors
if ($LASTEXITCODE -ne 0 -or -not $apimKey) { throw 'Unable to retrieve the APIM subscription key.' }

$env:APIM_SUBSCRIPTION_KEY = $apimKey
try {
    Invoke-Checked 'Validating deployed APIM configuration' {
        & $pythonCommand "$PSScriptRoot\..\validate_apim.py" `
            --resource-group $ResourceGroup `
            --service-name $ServiceName `
            --api-id $ApiId `
            --subscription-id $SubscriptionId
    }

    foreach ($mode in @('default', 'v1', 'legacy')) {
        Invoke-Checked "Running APIM $mode smoke test" {
            & $pythonCommand "$PSScriptRoot\..\smoke_test.py" --target apim --api-mode $mode
        }
    }

    Invoke-Checked 'Comparing legacy and v1 through APIM' {
        & $pythonCommand "$PSScriptRoot\..\compare_responses.py" --target apim
    }

    if (-not $SkipTelemetryCheck) {
        Write-Output "`n==> Checking sanitized Application Insights telemetry"
        $query = @'
traces
| where timestamp > ago(30m)
| where message == "OpenAI migration request routed by API mode."
| extend api_mode = tostring(customDimensions.api_mode)
| summarize requests=count(), modes=make_set(api_mode) by bin(timestamp, 5m)
| order by timestamp desc
'@
        $componentResourceId = (
            "/subscriptions/$SubscriptionId/resourceGroups/$ResourceGroup" +
            "/providers/Microsoft.Insights/components/$ApplicationInsightsName"
        )
        $queryBodyPath = Join-Path $env:TEMP "application-insights-query-$PID.json"
        try {
            $queryBody = @{ query = $query } | ConvertTo-Json -Compress
            [IO.File]::WriteAllText($queryBodyPath, $queryBody, [Text.UTF8Encoding]::new($false))
            $telemetryJson = az rest `
                --method post `
                --uri "https://management.azure.com${componentResourceId}/query?api-version=2018-04-20" `
                --body "@$queryBodyPath" `
                --headers 'Content-Type=application/json' `
                --subscription $SubscriptionId `
                --query 'tables[0].rows' `
                --output json `
                --only-show-errors 2>$null
            if ($LASTEXITCODE -ne 0) {
                Write-Warning 'Live requests passed, but telemetry could not be read. Grant the caller Application Insights data read access or use -SkipTelemetryCheck.'
            }
            elseif (@($telemetryJson | ConvertFrom-Json).Count -eq 0) {
                Write-Warning 'Live requests passed; sanitized traces are not visible yet because ingestion can be delayed.'
            }
            else {
                $telemetryJson
            }
        }
        finally {
            Remove-Item $queryBodyPath -Force -ErrorAction SilentlyContinue
        }
    }

    Write-Output "`nLive migration validation passed for APIM '$ServiceName'."
}
finally {
    Remove-Item Env:APIM_SUBSCRIPTION_KEY -ErrorAction SilentlyContinue
    $apimKey = $null
}