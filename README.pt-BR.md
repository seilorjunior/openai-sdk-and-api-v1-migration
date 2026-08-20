# POC de migração para OpenAI SDK e API v1

[English](README.md) | **Português (Brasil)**

POC baseada no aviso de descontinuação do Azure AI Inference SDK em 26/08/2026. Ela comprova a troca de `azure-ai-inference` + `/models` por `openai` + `/openai/v1` e provisiona uma fachada temporária no Azure API Management (APIM) para comparar v1 e a API Azure OpenAI versionada.

## Escopo

1. Chamada com o SDK estável `openai`, usando `model=<nome-do-deployment>` e sem `api-version`.
2. Smoke test direto no Azure OpenAI e pelo gateway APIM.
3. Streaming, Responses API, tools, structured outputs, embeddings, imagens, áudio, batch e fine-tuning com execução opt-in.
4. Retry de `429`, timeout, cancelamento, segurança, carga limitada, custo estimado e comparação comportamental legado/v1.
5. APIM com operação de chat dual-mode, identidade gerenciada, rate limit, retry, produto/assinatura, logs e alerta de falhas.
6. Gates determinísticos em pull requests e testes live manuais protegidos por ambiente.
7. Nenhuma chave, prompt ou resposta é exportada pelo validador e relatórios operacionais.

## Arquitetura

```text
Aplicação Python
  |-- OpenAI SDK direto --> <recurso>.openai.azure.com/openai/v1/*
  |
  |-- APIM v1 --> <apim>.azure-api.net/openai/v1/*
  |                 `-- identidade gerenciada, audiência https://ai.azure.com
  |
  `-- APIM chat dual --> <apim>.azure-api.net/openai/v1/chat/completions
    |-- X-API-Mode ausente ou v1
    |     `-- /openai/v1/chat/completions
    |          audiência https://ai.azure.com
    `-- X-API-Mode: legacy
      `-- /openai/deployments/<deployment>/chat/completions?api-version=2024-10-21
           audiência https://cognitiveservices.azure.com

Todos os caminhos implantados chegam ao mesmo deployment no recurso Azure OpenAI.
```

A política dual cobre somente a operação existente de chat. Ela não faz fallback em caso de falha: header ausente preserva o backend v1, enquanto um valor não vazio diferente de `v1|legacy` retorna `400 invalid_api_mode`.

## Estrutura do repositório

| Caminho | Finalidade |
| --- | --- |
| `infra/` | Bicep de escopo de assinatura e ARM compilado para Azure OpenAI, APIM, observabilidade e RBAC. |
| `samples/` | Policies APIM de referência para v1 e chat dual-mode. |
| `scripts/` | Gate live, rotação de chave, promoção de revisão e remoção segura da operação obsoleta. |
| `tests/` | Testes determinísticos executados localmente e no CI. |
| `smoke_test.py` | Smoke tests direto/APIM para os modos padrão, v1 e legado. |
| `capability_test.py` | Probes opt-in das capacidades da API v1. |
| `compare_responses.py` | Comparação comportamental sem registrar o texto gerado. |
| `load_test.py` | Teste de carga limitado com latência, tokens e custo opcional. |
| `validate_apim.py` | Validação live ou offline da configuração APIM. |

## Pré-requisitos

- Python 3.10 ou posterior.
- Azure CLI e Azure Developer CLI (`azd`) autenticadas.
- Permissão para criar os recursos descritos em `infra/` e atribuir papéis RBAC.
- Para o smoke test: deployment ativo, chave do APIM e identidade Entra com acesso no teste direto.
- A identidade gerenciada do APIM deve possuir `Cognitive Services User` no recurso de IA.
- PowerShell 7 para os scripts operacionais em `scripts/`.

## Instalação e testes locais

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
az bicep build --file infra/main.bicep --outfile infra/main.json
```

Os testes unitários e a compilação do Bicep são determinísticos e não exigem recursos Azure live. Esses mesmos checks são executados pelo workflow `Migration validation` em pushes para `main` e pull requests.

## Provisionar com Azure Developer CLI

O projeto inclui Bicep compatível com `azd` para criar um ambiente isolado com Azure OpenAI, deployment `gpt-4.1-mini`, APIM Developer, identidade gerenciada, Application Insights, Log Analytics, alertas e RBAC.

```powershell
azd auth login
azd env new '<nome-do-ambiente>' --no-prompt
azd env set AZURE_SUBSCRIPTION_ID '<subscription-id>'
azd env set AZURE_LOCATION 'brazilsouth'
azd env set APIM_LOCATION 'centralus'
azd env set APIM_PUBLISHER_EMAIL '<email-do-publisher>'
azd env set TELEMETRY_READER_PRINCIPAL_ID '<object-id-do-leitor-de-telemetria>'
azd provision
```

`TELEMETRY_READER_PRINCIPAL_ID` é opcional. Quando definido, o Bicep concede `Monitoring Reader` somente no componente Application Insights; não é necessário conceder acesso ao workspace Log Analytics. Para o usuário autenticado, obtenha o object ID com `az ad signed-in-user show --query id -o tsv`.

O provisionamento do APIM pode levar vários minutos. Ao concluir, o `azd` grava endpoints e nomes não secretos no ambiente. Carregue-os na sessão atual e alinhe o Azure CLI à mesma assinatura:

```powershell
azd env get-values | ForEach-Object {
  if ($_ -match '^([^=]+)="(.*)"$') {
    [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2], 'Process')
  }
}
az account set --subscription $env:AZURE_SUBSCRIPTION_ID
```

As variáveis com escopo `Process` existem somente nesse terminal. Repita o bloco ao abrir outra sessão.

Recupere somente a chave da assinatura APIM para a sessão do terminal. A conta Azure OpenAI tem autenticação local desabilitada; chamadas diretas e o backend APIM usam Microsoft Entra ID.

```powershell
$env:APIM_SUBSCRIPTION_KEY = az rest --method post `
  --uri "$(az apim show --resource-group "rg-$(azd env get-value AZURE_ENV_NAME)" --name $env:APIM_SERVICE_NAME --query id -o tsv)/subscriptions/$env:APIM_SUBSCRIPTION_ID/listSecrets?api-version=2024-05-01" `
  --query primaryKey -o tsv
```

Para remover o ambiente inteiro após a POC, use `azd down` e revise os recursos antes de confirmar.

## Validar a configuração do APIM

Use o identificador interno da API, não o display name:

```powershell
python .\validate_apim.py `
  --resource-group '<resource-group>' `
  --service-name '<apim-name>' `
  --api-id '<api-id>' `
  --export '.\apim-snapshot.json'
```

O comando retorna `0` quando não encontra incompatibilidades e `1` quando encontra erros. O snapshot pode ser revisado sem acesso ao Azure:

```powershell
python .\validate_apim.py --snapshot .\apim-snapshot.json
```

Critérios obrigatórios: rota ou rewrite com `/openai/v1`, operação `chat/completions`, ausência de `/models` e autenticação de backend por identidade gerenciada ou `api-key`. `api-version` e `/openai/deployments/` são permitidos somente no branch `legacy` completo da policy dual. Consulte [Política APIM para OpenAI API v1](docs/apim-policy.md) para os detalhes.

## Executar smoke tests

### Direto no Azure OpenAI

```powershell
$env:AZURE_OPENAI_BASE_URL = 'https://<recurso>.openai.azure.com/openai/v1/'
$env:AZURE_OPENAI_DEPLOYMENT = '<nome-do-deployment>'
python .\smoke_test.py --target direct
```

O cliente usa `DefaultAzureCredential` com o escopo `https://ai.azure.com/.default`. Configure `OPENAI_TIMEOUT_SECONDS` e `OPENAI_MAX_RETRIES` quando os padrões de 30 segundos e duas tentativas adicionais não forem adequados. Para verificar cancelamento assíncrono:

```powershell
python .\smoke_test.py --target direct --cancel-after 0.1
```

### Pelo APIM

```powershell
$env:APIM_OPENAI_BASE_URL = 'https://<apim>.azure-api.net/openai/v1/'
$env:APIM_SUBSCRIPTION_KEY = '<subscription-key>'
$env:AZURE_OPENAI_DEPLOYMENT = '<nome-do-deployment>'
python .\smoke_test.py --target apim
```

O base URL deve terminar no prefixo que expõe a API v1. A policy remove o `Authorization` técnico criado pelo SDK e autentica o backend com a identidade gerenciada do APIM.

## Manter legado e v1 durante a transição

O parâmetro `--api-mode` seleciona o protocolo sem misturar URLs, escopos de autenticação ou SDKs. Configure o endpoint legado completo, incluindo `/models`:

```powershell
$env:LEGACY_MODELS_BASE_URL = 'https://<recurso>.services.ai.azure.com/models'
$env:AZURE_OPENAI_DEPLOYMENT = '<nome-do-deployment>'

python .\smoke_test.py --api-mode legacy --target direct
python .\smoke_test.py --api-mode v1 --target direct
```

Pelo APIM, a operação existente `/openai/v1/chat/completions` aceita seleção de backend. Header ausente ou `X-API-Mode: v1` mantém v1; `X-API-Mode: legacy` seleciona a rota versionada no mesmo recurso Azure OpenAI. O smoke test configura o header automaticamente:

```powershell
python .\smoke_test.py --api-mode default --target apim
python .\smoke_test.py --api-mode v1 --target apim
python .\smoke_test.py --api-mode legacy --target apim
python .\compare_responses.py --target apim
```

Para comparar os dois caminhos diretos sem registrar o texto gerado:

```powershell
python .\compare_responses.py --target direct
```

Somente chat é dual-mode. Responses, embeddings, imagens, áudio, files, batches, fine-tuning, cancelamento e os testes de capacidades avançadas permanecem exclusivos de v1.

## Capacidades, resiliência e carga

Liste as opções com `python .\capability_test.py --help`. Capacidades que precisam de outro deployment ou arquivo retornam `skipped`; batch e fine-tuning só criam jobs com `--execute-mutating`.

```powershell
python .\capability_test.py --target direct --capability all
$env:OPENAI_SAFETY_PROMPT = '<prompt-de-teste-aprovado>'
python .\capability_test.py --target apim --capability safety
```

O teste de carga impõe no máximo 10.000 requisições por modo e concorrência 100. Acima de 1.000 por modo, exige `--confirm-large-load`. O relatório inclui percentis, tokens, falhas por tipo e custo somente quando as tarifas aprovadas são fornecidas:

```powershell
$env:OPENAI_INPUT_USD_PER_1M_TOKENS = '<tarifa>'
$env:OPENAI_OUTPUT_USD_PER_1M_TOKENS = '<tarifa>'
python .\load_test.py --target apim --api-mode both --requests 20 --concurrency 4
```

`--requests` é aplicado a cada modo. O exemplo gera 20 chamadas v1 e depois 20 chamadas legacy. O processo retorna código diferente de zero se qualquer chamada falhar.

## Operação e entrega

### Gate live local

Depois de `azd provision`, execute todos os gates live sem imprimir ou persistir a chave APIM:

```powershell
.\scripts\validate-live-migration.ps1
```

O script resolve os outputs do ambiente `azd`, recupera a chave somente em memória, valida o APIM, executa smoke tests `default`, `v1` e `legacy`, compara os dois modos e consulta traces sanitizados no Application Insights. Use `-SkipTelemetryCheck` somente quando a identidade não tiver `Monitoring Reader` no componente.

### GitHub Actions

O workflow manual `Live migration gate` usa o ambiente protegido `openai-migration-live`. Configure as variáveis `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`, `AZURE_OPENAI_BASE_URL`, `LEGACY_MODELS_BASE_URL`, `AZURE_OPENAI_DEPLOYMENT` e `APIM_OPENAI_BASE_URL`. Configure `APIM_SUBSCRIPTION_KEY` como secret. A autenticação Azure usa federação OIDC; não armazene client secrets.

### Limpeza da operação obsoleta

Deployments ARM incrementais não excluem operações removidas do Bicep. Execute a limpeza uma vez, somente depois de provisionar e validar a policy dual-mode:

```powershell
.\scripts\remove-obsolete-apim-operation.ps1 -ResourceGroup $env:AZURE_RESOURCE_GROUP `
  -ServiceName $env:APIM_SERVICE_NAME -ApiId $env:APIM_API_ID -WhatIf

.\scripts\remove-obsolete-apim-operation.ps1 -ResourceGroup $env:AZURE_RESOURCE_GROUP `
  -ServiceName $env:APIM_SERVICE_NAME -ApiId $env:APIM_API_ID -Confirm:$false
```

### Rotação de chave e promoção de revisão

O Bicep provisiona produto e assinatura APIM, mas não retorna as chaves. O script abaixo regenera um slot, atualiza o secret de um ambiente GitHub com `gh`, executa o smoke test e somente então invalida o slot antigo:

```powershell
.\scripts\rotate-apim-key.ps1 -ResourceGroup '<rg>' -ServiceName '<apim>' `
  -SubscriptionId $env:APIM_SUBSCRIPTION_ID -NewKeySlot secondary `
  -GitHubEnvironment openai-migration-live
```

Promova uma revisão candidata somente após configurá-la. O comando testa a URL `;rev=N`, promove, repete smoke e carga limitada e restaura automaticamente a revisão estável em qualquer falha:

```powershell
.\scripts\promote-apim-revision.ps1 -ResourceGroup '<rg>' -ServiceName '<apim>' `
  -ApiId $env:APIM_API_ID -StableRevision 1 -CandidateRevision 2
```

O Log Analytics recebe logs e métricas do APIM, e um alerta notifica o publisher quando há mais de cinco falhas em cinco minutos. Prompts, respostas, tokens e chaves não devem ser adicionados às policies de logging.

## Critérios para retirar o legado

- 14 dias consecutivos sem requisições `legacy` de clientes ativos;
- taxa de sucesso v1 de pelo menos 99,5% durante a janela;
- latência p95 v1 no máximo 10% acima do baseline legado aprovado, excluindo incidentes gerais do provedor;
- testes de capacidades e comparação APIM-vs-APIM aprovados, com aceite das diferenças intencionais;
- rollback ensaiado e capaz de restaurar a policy anterior em até 15 minutos sem mudar a URL pública;
- aprovação formal do responsável pelo cutover.

Após o cutover, mantenha o branch legado desabilitado, mas recuperável, por sete dias. Ao fim desse período, remova-o por Bicep.

## Rede privada e produção

Para rede privada, forneça sub-redes dedicadas e a VNet antes do provisionamento. O Bicep injeta APIM na VNet, cria o private endpoint e DNS do Azure OpenAI e desabilita seu acesso público:

```powershell
azd env set ENABLE_PRIVATE_NETWORKING true
azd env set APIM_SUBNET_RESOURCE_ID '<subnet-resource-id>'
azd env set PRIVATE_ENDPOINT_SUBNET_RESOURCE_ID '<subnet-resource-id>'
azd env set VIRTUAL_NETWORK_RESOURCE_ID '<vnet-resource-id>'
```

O SKU Developer é adequado à POC, mas não possui SLA de produção. Antes de tráfego de cliente, escolha um SKU com SLA/capacidade compatíveis, valide DNS/443 a partir do gateway e defina limites aprovados de erro e latência.

O gate é um canário sintético, não uma divisão percentual de tráfego. Para canário ponderado, mantenha APIs/backends paralelos e aplique roteamento por coorte ou porcentagem em uma camada aprovada para produção.

## Critérios de aceite

- O validador termina com zero erros.
- As chamadas direta e via APIM retornam conteúdo não vazio para o mesmo deployment.
- Nenhuma requisição v1 contém `api-version` nem usa `/models`.
- O branch legado usa `api-version=2024-10-21` somente na policy APIM e nunca expõe esse detalhe no contrato público.
- Uma chave APIM inválida retorna `401` ou `403`.
- O APIM autentica no backend com identidade gerenciada e audiência `https://ai.azure.com`.
- O log do APIM registra status e latência sem capturar prompt, resposta ou chaves.

## Evidências da POC

Registre o relatório do validador, horário/status/latência dos smoke tests, request ID do APIM e uma captura da atribuição RBAC. Não inclua chaves, tokens, prompts reais de cliente ou respostas sensíveis.

## Referências oficiais

- [Migração do Azure AI Inference SDK para OpenAI SDK](https://learn.microsoft.com/azure/foundry/how-to/model-inference-to-openai-migration)
- [Ciclo de vida da API v1](https://learn.microsoft.com/azure/foundry/openai/api-version-lifecycle)
- [Autenticação do Azure OpenAI no APIM](https://learn.microsoft.com/azure/api-management/api-management-authenticate-authorize-azure-openai)
