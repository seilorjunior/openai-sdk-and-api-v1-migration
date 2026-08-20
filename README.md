# OpenAI SDK and API v1 migration POC

**English** | [Português (Brasil)](README.pt-BR.md)

This proof of concept addresses the Azure AI Inference SDK retirement announced for August 26, 2026. It demonstrates the migration from `azure-ai-inference` + `/models` to `openai` + `/openai/v1` and provisions a temporary Azure API Management (APIM) facade for comparing v1 with the versioned Azure OpenAI API.

## Scope

1. Calls through the stable `openai` SDK using `model=<deployment-name>` without `api-version`.
2. Direct Azure OpenAI and APIM gateway smoke tests.
3. Opt-in checks for streaming, the Responses API, tools, structured outputs, embeddings, images, audio, batch, and fine-tuning.
4. Checks for `429` retries, timeouts, cancellation, safety, bounded load, estimated cost, and legacy/v1 behavioral parity.
5. APIM with one dual-mode chat operation, managed identity, rate limiting, retries, product/subscription access, logs, and failure alerts.
6. Deterministic pull-request gates and protected manual live tests.
7. Validators and operational reports that do not export keys, prompts, or responses.

## Architecture

```text
Python application
  |-- OpenAI SDK direct --> <resource>.openai.azure.com/openai/v1/*
  |
  |-- APIM v1 --> <apim>.azure-api.net/openai/v1/*
  |                 `-- managed identity, audience https://ai.azure.com
  |
  `-- APIM dual chat --> <apim>.azure-api.net/openai/v1/chat/completions
    |-- X-API-Mode missing or v1
    |     `-- /openai/v1/chat/completions
    |          audience https://ai.azure.com
    `-- X-API-Mode: legacy
      `-- /openai/deployments/<deployment>/chat/completions?api-version=2024-10-21
           audience https://cognitiveservices.azure.com

All deployed paths reach the same model deployment in the Azure OpenAI resource.
```

The dual policy applies only to the existing chat operation. It does not fall back after a failure: a missing header preserves the v1 backend, while any non-empty value other than `v1|legacy` returns `400 invalid_api_mode`.

## Repository layout

| Path | Purpose |
| --- | --- |
| `infra/` | Subscription-scoped Bicep and compiled ARM for Azure OpenAI, APIM, observability, and RBAC. |
| `samples/` | Reference APIM policies for v1 and dual-mode chat. |
| `scripts/` | Live gate, key rotation, revision promotion, and safe obsolete-operation removal. |
| `tests/` | Deterministic tests run locally and in CI. |
| `smoke_test.py` | Direct/APIM smoke tests for default, v1, and legacy modes. |
| `capability_test.py` | Opt-in probes for OpenAI API v1 capabilities. |
| `compare_responses.py` | Behavioral comparison without logging generated text. |
| `load_test.py` | Bounded load test with latency, token, and optional cost reporting. |
| `validate_apim.py` | Live or offline APIM configuration validation. |

## Prerequisites

- Python 3.10 or later.
- Authenticated Azure CLI and Azure Developer CLI (`azd`).
- Permission to create the resources in `infra/` and assign RBAC roles.
- For smoke tests: an active deployment, an APIM key, and an Entra identity with access for direct testing.
- The APIM managed identity must have `Cognitive Services User` on the AI resource.
- PowerShell 7 for the operational scripts in `scripts/`.

## Install and test locally

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
az bicep build --file infra/main.bicep --outfile infra/main.json
```

The unit tests and Bicep build are deterministic and do not require live Azure resources. The `Migration validation` workflow runs the same checks on pushes to `main` and pull requests.

## Provision with Azure Developer CLI

The project includes `azd`-compatible Bicep that creates an isolated environment with Azure OpenAI, a `gpt-4.1-mini` deployment, APIM Developer, a managed identity, Application Insights, Log Analytics, alerts, and RBAC.

```powershell
azd auth login
azd env new '<environment-name>' --no-prompt
azd env set AZURE_SUBSCRIPTION_ID '<subscription-id>'
azd env set AZURE_LOCATION 'brazilsouth'
azd env set APIM_LOCATION 'centralus'
azd env set APIM_PUBLISHER_EMAIL '<publisher-email>'
azd env set TELEMETRY_READER_PRINCIPAL_ID '<telemetry-reader-object-id>'
azd provision
```

`TELEMETRY_READER_PRINCIPAL_ID` is optional. When set, Bicep grants `Monitoring Reader` only on the Application Insights component; access to the Log Analytics workspace is not required. Retrieve the signed-in user's object ID with `az ad signed-in-user show --query id -o tsv`.

APIM provisioning can take several minutes. When it completes, `azd` stores non-secret endpoints and resource names in the environment. Load them into the current session and select the same Azure subscription:

```powershell
azd env get-values | ForEach-Object {
  if ($_ -match '^([^=]+)="(.*)"$') {
    [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2], 'Process')
  }
}
az account set --subscription $env:AZURE_SUBSCRIPTION_ID
```

Variables with `Process` scope exist only in that terminal. Repeat the block in each new session.

Retrieve only the APIM subscription key into the terminal process. Local authentication is disabled on the Azure OpenAI account; direct calls and the APIM backend use Microsoft Entra ID.

```powershell
$env:APIM_SUBSCRIPTION_KEY = az rest --method post `
  --uri "$(az apim show --resource-group "rg-$(azd env get-value AZURE_ENV_NAME)" --name $env:APIM_SERVICE_NAME --query id -o tsv)/subscriptions/$env:APIM_SUBSCRIPTION_ID/listSecrets?api-version=2024-05-01" `
  --query primaryKey -o tsv
```

After the POC, use `azd down` to remove the environment and review the resources before confirming deletion.

## Validate APIM configuration

Use the internal API identifier, not its display name:

```powershell
python .\validate_apim.py `
  --resource-group '<resource-group>' `
  --service-name '<apim-name>' `
  --api-id '<api-id>' `
  --export '.\apim-snapshot.json'
```

The command returns `0` when it finds no incompatibilities and `1` when it finds errors. You can review the snapshot without Azure access:

```powershell
python .\validate_apim.py --snapshot .\apim-snapshot.json
```

Required criteria include a route or rewrite containing `/openai/v1`, a `chat/completions` operation, no `/models`, and backend authentication through managed identity or `api-key`. `api-version` and `/openai/deployments/` are allowed only in a complete `legacy` branch of the dual policy. See [APIM policy for OpenAI API v1](docs/apim-policy.md) for details. This linked policy guide is currently available in Portuguese.

## Run smoke tests

### Direct Azure OpenAI

```powershell
$env:AZURE_OPENAI_BASE_URL = 'https://<resource>.openai.azure.com/openai/v1/'
$env:AZURE_OPENAI_DEPLOYMENT = '<deployment-name>'
python .\smoke_test.py --target direct
```

The client uses `DefaultAzureCredential` with the `https://ai.azure.com/.default` scope. Set `OPENAI_TIMEOUT_SECONDS` and `OPENAI_MAX_RETRIES` when the defaults of 30 seconds and two additional attempts are not appropriate. To test asynchronous cancellation:

```powershell
python .\smoke_test.py --target direct --cancel-after 0.1
```

### Through APIM

```powershell
$env:APIM_OPENAI_BASE_URL = 'https://<apim>.azure-api.net/openai/v1/'
$env:APIM_SUBSCRIPTION_KEY = '<subscription-key>'
$env:AZURE_OPENAI_DEPLOYMENT = '<deployment-name>'
python .\smoke_test.py --target apim
```

The base URL must end with the prefix exposing API v1. The policy removes the technical `Authorization` header created by the SDK and authenticates the backend through the APIM managed identity.

## Keep legacy and v1 during transition

The `--api-mode` option selects the protocol without mixing URLs, authentication scopes, or SDKs. Set the complete legacy endpoint, including `/models`:

```powershell
$env:LEGACY_MODELS_BASE_URL = 'https://<resource>.services.ai.azure.com/models'
$env:AZURE_OPENAI_DEPLOYMENT = '<deployment-name>'

python .\smoke_test.py --api-mode legacy --target direct
python .\smoke_test.py --api-mode v1 --target direct
```

Through APIM, the existing `/openai/v1/chat/completions` operation supports backend selection. A missing header or `X-API-Mode: v1` keeps v1; `X-API-Mode: legacy` selects the versioned route on the same Azure OpenAI resource. The smoke test sets the header automatically:

```powershell
python .\smoke_test.py --api-mode default --target apim
python .\smoke_test.py --api-mode v1 --target apim
python .\smoke_test.py --api-mode legacy --target apim
python .\compare_responses.py --target apim
```

To compare both direct paths without logging generated text:

```powershell
python .\compare_responses.py --target direct
```

Only chat is dual-mode. Responses, embeddings, images, audio, files, batches, fine-tuning, cancellation, and advanced capability tests remain v1-only.

## Capabilities, resilience, and load

List options with `python .\capability_test.py --help`. Capabilities that require another deployment or file return `skipped`; batch and fine-tuning create jobs only with `--execute-mutating`.

```powershell
python .\capability_test.py --target direct --capability all
$env:OPENAI_SAFETY_PROMPT = '<approved-test-prompt>'
python .\capability_test.py --target apim --capability safety
```

The load test allows at most 10,000 requests per mode with concurrency capped at 100. Runs above 1,000 requests per mode require `--confirm-large-load`. Reports include percentiles, tokens, failure types, and cost only when approved rates are supplied:

```powershell
$env:OPENAI_INPUT_USD_PER_1M_TOKENS = '<rate>'
$env:OPENAI_OUTPUT_USD_PER_1M_TOKENS = '<rate>'
python .\load_test.py --target apim --api-mode both --requests 20 --concurrency 4
```

`--requests` applies to each mode. The example makes 20 v1 calls followed by 20 legacy calls. The process returns a non-zero exit code if any request fails.

## Operations and delivery

### Local live gate

After `azd provision`, run all live gates without printing or persisting the APIM key:

```powershell
.\scripts\validate-live-migration.ps1
```

The script resolves `azd` outputs, holds the key only in memory, validates APIM, runs `default`, `v1`, and `legacy` smoke tests, compares both modes, and queries sanitized Application Insights traces. Use `-SkipTelemetryCheck` only when the identity does not have `Monitoring Reader` on the component.

### GitHub Actions

The manual `Live migration gate` workflow uses the protected `openai-migration-live` environment. Configure the variables `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`, `AZURE_OPENAI_BASE_URL`, `LEGACY_MODELS_BASE_URL`, `AZURE_OPENAI_DEPLOYMENT`, and `APIM_OPENAI_BASE_URL`. Configure `APIM_SUBSCRIPTION_KEY` as a secret. Azure authentication uses OIDC federation; do not store client secrets.

### Remove the obsolete operation

Incremental ARM deployments do not delete operations removed from Bicep. Run this cleanup once, only after provisioning and validating the dual-mode policy:

```powershell
.\scripts\remove-obsolete-apim-operation.ps1 -ResourceGroup $env:AZURE_RESOURCE_GROUP `
  -ServiceName $env:APIM_SERVICE_NAME -ApiId $env:APIM_API_ID -WhatIf

.\scripts\remove-obsolete-apim-operation.ps1 -ResourceGroup $env:AZURE_RESOURCE_GROUP `
  -ServiceName $env:APIM_SERVICE_NAME -ApiId $env:APIM_API_ID -Confirm:$false
```

### Rotate keys and promote revisions

Bicep provisions the APIM product and subscription but does not return their keys. This script regenerates one slot, updates a GitHub environment secret through `gh`, runs the smoke test, and only then invalidates the old slot:

```powershell
.\scripts\rotate-apim-key.ps1 -ResourceGroup '<rg>' -ServiceName '<apim>' `
  -SubscriptionId $env:APIM_SUBSCRIPTION_ID -NewKeySlot secondary `
  -GitHubEnvironment openai-migration-live
```

Promote a candidate revision only after configuring it. The command tests the `;rev=N` URL, promotes it, repeats bounded smoke/load checks, and automatically restores the stable revision after any failure:

```powershell
.\scripts\promote-apim-revision.ps1 -ResourceGroup '<rg>' -ServiceName '<apim>' `
  -ApiId $env:APIM_API_ID -StableRevision 1 -CandidateRevision 2
```

Log Analytics receives APIM logs and metrics, and an alert notifies the publisher after more than five failures in five minutes. Do not add prompts, responses, tokens, or keys to logging policies.

## Legacy retirement criteria

- 14 consecutive days without `legacy` requests from active clients;
- v1 success rate of at least 99.5% during that window;
- v1 p95 latency no more than 10% above the approved legacy baseline, excluding provider-wide incidents;
- passing capability and APIM-to-APIM comparison tests, with intentional differences accepted;
- a rehearsed rollback that restores the previous policy within 15 minutes without changing the public URL;
- formal approval from the cutover owner.

After cutover, keep the legacy branch disabled but recoverable for seven days. Remove it through Bicep after that period.

## Private networking and production

For private networking, provide dedicated subnets and the VNet before provisioning. Bicep injects APIM into the VNet, creates the Azure OpenAI private endpoint and DNS, and disables public access to Azure OpenAI:

```powershell
azd env set ENABLE_PRIVATE_NETWORKING true
azd env set APIM_SUBNET_RESOURCE_ID '<subnet-resource-id>'
azd env set PRIVATE_ENDPOINT_SUBNET_RESOURCE_ID '<subnet-resource-id>'
azd env set VIRTUAL_NETWORK_RESOURCE_ID '<vnet-resource-id>'
```

The Developer SKU is suitable for this POC but has no production SLA. Before customer traffic, choose a SKU with suitable SLA and capacity, validate DNS and TCP 443 from the gateway, and define approved error and latency thresholds.

The live gate is a synthetic canary, not percentage-based traffic splitting. For weighted canary delivery, keep parallel APIs/backends and route by cohort or percentage in a production-approved layer.

## Acceptance criteria

- The validator exits with zero errors.
- Direct and APIM calls return non-empty content from the same deployment.
- No v1 request contains `api-version` or uses `/models`.
- The legacy branch uses `api-version=2024-10-21` only inside the APIM policy and never exposes it in the public contract.
- An invalid APIM key returns `401` or `403`.
- APIM authenticates to the backend through managed identity with the `https://ai.azure.com` audience.
- APIM logs status and latency without capturing prompts, responses, or keys.

## POC evidence

Record the validator report, smoke-test timestamp/status/latency, APIM request ID, and a screenshot of the RBAC assignment. Do not include keys, tokens, real customer prompts, or sensitive responses.

## Official references

- [Migrate from Azure AI Inference SDK to OpenAI SDK](https://learn.microsoft.com/azure/foundry/how-to/model-inference-to-openai-migration)
- [API v1 lifecycle](https://learn.microsoft.com/azure/foundry/openai/api-version-lifecycle)
- [Authenticate Azure OpenAI from APIM](https://learn.microsoft.com/azure/api-management/api-management-authenticate-authorize-azure-openai)
