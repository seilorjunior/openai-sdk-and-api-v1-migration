# OpenAI v1 migration runbook

This runbook covers operational response for the temporary APIM migration facade. Run commands from the repository root with an authenticated Azure CLI, Azure Developer CLI, Python environment, and PowerShell 7 session. Never paste subscription keys, prompts, responses, or tokens into tickets or logs.

## First response

1. Record the alert time, environment, APIM service, API ID, affected route, correlation IDs, and the last known good revision.
2. Check Azure Service Health and Azure OpenAI availability before changing the gateway.
3. Run the bounded live gate:

   ```powershell
   .\scripts\validate-live-migration.ps1
   ```

4. Stop promotion or retirement when smoke, parity, telemetry, or readiness checks fail.

## Deterministic quality gate

Run the local quality gate before changing an APIM revision, migration policy, client behavior, or retirement criteria:

```powershell
python -m pytest --cov --cov-branch --cov-report=term-missing --cov-report=xml --cov-fail-under=65
python -m ruff check .
python -m mypy
```

The pytest suite is offline and mocks SDK, Azure CLI, and process boundaries. It validates smoke and cancellation lifecycle behavior, v1 capability adapters, response parity, thread-local load client isolation, migration findings, retirement evidence, retry handling, APIM validation, and secret redaction. A failure below 65% branch coverage blocks the change. `coverage.xml` is generated for tooling and is ignored by Git.

Live smoke, parity, load, telemetry, and retirement checks remain separate because they require deployed Azure resources and protected credentials. Run `scripts/validate-live-migration.ps1` after the deterministic gate passes.

## Migration MCP validation

Validate the MCP path as four separate gates. A successful health check or MCP initialization does not prove APIM tool discovery.

1. Confirm `https://<web-app>.azurewebsites.net/health` returns `{"status":"ok"}`.
2. Call the App Service `/mcp` endpoint with `x-mcp-backend-key` from a protected operator environment and confirm the three expected tools. Never attach that key to evidence.
3. Confirm the APIM endpoint rejects a missing `Ocp-Apim-Subscription-Key`, then initializes with a valid key.
4. Run the resource-contract and live tool-discovery validator:

   ```powershell
   $env:APIM_SUBSCRIPTION_KEY = '<protected-product-key>'
   python .\validate_apim.py `
     --resource-group '<resource-group>' `
     --service-name '<apim-service>' `
     --api-id '<migration-mcp-api-id>' `
     --mcp-url 'https://<apim-service>.azure-api.net/migration-mcp/mcp'
   Remove-Item Env:APIM_SUBSCRIPTION_KEY
   ```

The command must report the documented endpoint array, retained `transportType: streamable`, and exactly `list_migration_rules`, `scan_migration_sources`, and `evaluate_retirement_readiness`. On the Central US Developer/STV2 stamp tested August 26, 2026, ARM instead returns a dictionary endpoint shape without `transportType`, and APIM `tools/list` returns zero tools even though the backend receives `ListToolsRequest` and answers successfully. `Microsoft.ApiManagement` provider registration did not resolve the mismatch.

Do not change production to reproduce this condition. Before replacing the dictionary workaround in Bicep, create a disposable MCP API and prove that the documented array contract is accepted and retained on ARM GET, then prove `tools/list` through APIM. For Azure support, include UTC timestamps, subscription/resource group/APIM/API identifiers, region/SKU/platform version, redacted initialize and `tools/list` responses, the ARM round-trip shape, direct-backend tool-list success, and sanitized App Service evidence that `ListToolsRequest` returned HTTP 200. Exclude all subscription and backend keys.

## Failed-request alert triage

The default alert fires when APIM records more than five failed requests in five minutes. Query Application Insights without request or response bodies:

```kusto
requests
| where timestamp > ago(30m)
| summarize requests=count(), failures=countif(success == false),
    throttles=countif(resultCode == "429"), p95=percentile(duration, 95)
    by bin(timestamp, 5m), resultCode, operation_Name
| order by timestamp desc
```

Classify the result before remediation:

| Signal | Likely owner | Next action |
| --- | --- | --- |
| `401` or `403` at APIM | Subscription or policy authentication | Validate the client key, APIM product/subscription state, managed identity, and `Cognitive Services User` assignment. |
| Backend `401` or `403` | APIM managed identity/RBAC | Verify the APIM user-assigned identity and backend token audience `https://ai.azure.com`. |
| `429` | APIM rate limit or model quota | Compare APIM policy limits with Azure OpenAI quota; reduce concurrency or increase approved capacity. |
| `5xx` with direct path healthy | APIM policy, revision, or networking | Validate APIM, inspect the current revision, and roll back if the failure began after promotion. |
| `5xx` on direct and APIM paths | Azure OpenAI deployment or provider incident | Check deployment health, quota, and Service Health; do not mask the incident with repeated gateway retries. |
| Latency only | Capacity, retry, DNS, or network path | Compare direct/APIM p95, retry counts, private DNS, and connection health. |

Use `operation_Id` to correlate sanitized routing traces with requests. Escalate only with timestamps, status codes, aggregate latency, revision IDs, and redacted correlation IDs.

## Rollback

The revision promotion script automatically restores `StableRevision` when its post-promotion smoke or load check fails:

```powershell
.\scripts\promote-apim-revision.ps1 -ResourceGroup '<rg>' -ServiceName '<apim>' `
  -ApiId '<api-id>' -StableRevision '<known-good>' -CandidateRevision '<candidate>'
```

For an incident discovered later, use the same script to canary and restore the previous known-good revision. Set `CandidateRevision` to the previous revision and `StableRevision` to the currently active revision. Keep `APIM_OPENAI_BASE_URL` and `APIM_SUBSCRIPTION_KEY` in process environment variables, then verify `default`, `v1`, and `legacy` modes.

After rollback:

1. Confirm the public URL is unchanged.
2. Run `smoke_test.py` for all three modes and a bounded load test.
3. Confirm failed-request rate returns to baseline.
4. Record the restored revision and do not resume promotion until the cause is understood.

## APIM key rotation recovery

Use the dual key slots so one known-good key remains available:

```powershell
.\scripts\rotate-apim-key.ps1 -ResourceGroup '<rg>' -ServiceName '<apim>' `
  -SubscriptionId '<apim-subscription-id>' -NewKeySlot secondary `
  -GitHubEnvironment openai-migration-live
```

The script regenerates the selected slot, updates the protected GitHub environment secret, validates it, and only then invalidates the other slot. If validation fails, the old slot remains valid. Restore the GitHub secret to the still-valid slot, verify environment protection and `gh` authentication, then retry using the failed slot. Never print either key.

## Scaling and throttling

1. Identify whether throttling comes from the APIM `rate-limit-by-key` policy or Azure OpenAI quota.
2. Review `APIM_RATE_LIMIT_CALLS_PER_MINUTE`, `APIM_SKU_NAME`, `APIM_CAPACITY`, `AZURE_OPENAI_DEPLOYMENT_SKU`, and `AZURE_OPENAI_DEPLOYMENT_CAPACITY`.
3. Query the Model Capacities API before changing capacity. It accounts for subscription quota and current regional service capacity:

```powershell
$subscriptionId = (azd env get-value AZURE_SUBSCRIPTION_ID).Trim().Trim('"')
$token = az account get-access-token `
  --subscription $subscriptionId `
  --resource https://management.azure.com/ `
  --query accessToken -o tsv
$uri = "https://management.azure.com/subscriptions/$subscriptionId/providers/Microsoft.CognitiveServices/modelCapacities?api-version=2024-10-01&modelFormat=OpenAI&modelName=gpt-4.1-mini&modelVersion=2025-04-14"
$capacity = Invoke-RestMethod -Method Get -Uri $uri `
  -Headers @{ Authorization = "Bearer $token" }
$capacity.value |
  Where-Object {
    $_.location -eq 'brazilsouth' -and
    $_.properties.skuName -eq 'GlobalStandard'
  } |
  Select-Object location, @{ Name = 'availableCapacity'; Expression = { $_.properties.availableCapacity } }
```

1. Interpret `availableCapacity` as additional capacity that can be allocated now. For the existing deployment, add its current capacity to this value to calculate the maximum scale target. For a new deployment, use no more than `availableCapacity`.
2. The checked-in capacity is intentionally `10`. Treat a scale-up as a separate reviewed change rather than applying the maximum available quota automatically.
3. After an approved change, update the azd environment value, run `azd provision`, and repeat smoke, parity, and bounded load checks.
4. Increase APIM limits incrementally. Keep client retries responsible for `429`; APIM retries only transient `5xx` responses.

The Developer APIM SKU has no production SLA. Select a production SKU and capacity before serving customer traffic.

## Defender for AI Services verification

The subscription-scoped Bicep entry point changes Microsoft Defender for AI Services only when `ENABLE_DEFENDER_FOR_AI=true`. This explicit opt-in sets the subscription-wide `AI` plan to the billable `Standard` tier. Before enabling it or running `azd provision`, verify that the selected subscription is the intended billing and security boundary:

```powershell
$subscriptionId = az account show --query id -o tsv
azd env get-value AZURE_SUBSCRIPTION_ID
azd env get-value ENABLE_DEFENDER_FOR_AI
```

After provisioning, verify the effective plan and coverage:

```powershell
$uri = "https://management.azure.com/subscriptions/$subscriptionId/providers/Microsoft.Security/pricings/AI?api-version=2024-01-01"
az rest --method get --uri $uri `
  --query "{plan:name,tier:properties.pricingTier,coverage:properties.resourcesCoverageStatus,trial:properties.freeTrialRemainingTime}" `
  --output table
```

When enabled, expect `plan` to be `AI` and `tier` to be `Standard`. An `accepted` probe result validates the request contract only; inspect a controlled Defender XDR alert before marking enrichment verified. Investigate incomplete coverage before relying on that enrichment. Treat plan disablement as a security and billing change that requires explicit approval.

## Private-network troubleshooting

When `ENABLE_PRIVATE_NETWORKING=true`, all three resource IDs are mandatory: `APIM_SUBNET_RESOURCE_ID`, `PRIVATE_ENDPOINT_SUBNET_RESOURCE_ID`, and `VIRTUAL_NETWORK_RESOURCE_ID`. Bicep stops with a named validation error when any value is blank.

Check the path in this order:

1. Confirm the APIM subnet is dedicated and valid for the selected APIM SKU.
2. Confirm the private endpoint reports an approved connection to the Azure OpenAI account.
3. Confirm `privatelink.openai.azure.com` contains the account record and is linked to the supplied VNet.
4. Resolve the Azure OpenAI hostname from a host using the same VNet DNS path; it must resolve to the private endpoint address.
5. Test TCP 443 and then run the APIM smoke test. A successful DNS lookup alone does not prove routing or RBAC.
6. Verify NSG, route table, firewall, and custom DNS forwarding rules if APIM cannot reach the endpoint.

Do not re-enable public Azure OpenAI access as the first diagnostic step. Capture DNS results and effective network configuration before changing the topology.

## Cleanup and cost control

- Remove obsolete APIM operations with `scripts/remove-obsolete-apim-operation.ps1`; preview with `-WhatIf` first.
- Retain the recoverable legacy revision for seven days after approved cutover, then remove the legacy branch through Bicep.
- Review APIM capacity, Azure OpenAI provisioned capacity, Application Insights ingestion, and Log Analytics retention after each test wave.
- `azd down --purge` removes the optional DeepSeek account with the resource group, but it does not revert the subscription-wide Defender pricing plan.
- To disable Defender after explicit approval, update `Microsoft.Security/pricings/AI` to `pricingTier: Free` at subscription scope and verify the effective tier. Setting `ENABLE_DEFENDER_FOR_AI=false` only stops this template from managing the plan.
- Delete temporary parity, load, and retirement artifacts only after attaching the approved evidence to the change record.
- To remove the complete disposable environment, confirm the selected azd environment and evidence retention first, then run `azd down --purge`.

## Evidence and escalation checklist

Attach or record:

- UTC incident and recovery timestamps;
- environment, subscription, resource group, APIM service, API ID, and active revision;
- affected mode (`default-v1`, `v1`, or `legacy`) and sanitized correlation IDs;
- smoke, parity, capability, and bounded load outcomes;
- aggregate request count, failure rate, throttle rate, and p95 latency;
- `parity-report.json` and `retirement-report.json` when applicable;
- Azure Service Health status and any provider tracking ID;
- rollback action, restored revision, owner, and verification result;
- private DNS and connectivity evidence for network incidents.

Retirement approval requires at least 100 correlated v1 requests in the 14-day evidence window by default, a v1 request no older than 24 hours, no legacy requests for 14 days, the approved latency and success thresholds, successful parity, a rehearsed rollback, and owner approval. Keep the generated JSON as the decision record.
