# Política APIM para OpenAI API v1

Este documento explica a política de referência em [`samples/apim-policy.xml`](../samples/apim-policy.xml). O Bicep instancia a política para operações dedicadas de chat, Responses, embeddings, imagens, áudio, files, batches e fine-tuning.

## Resultado esperado

O cliente chama o gateway com a chave de assinatura do APIM. O gateway remove o cabeçalho de autorização enviado pelo OpenAI SDK, ajusta o caminho da requisição e obtém um token Microsoft Entra para acessar o recurso de IA.

```text
Cliente OpenAI SDK
  POST https://<apim>.azure-api.net/openai/v1/chat/completions
  Ocp-Apim-Subscription-Key: <chave-apim>
                 |
                 v
Azure API Management
  1. Valida a assinatura do APIM
  2. Remove o cabeçalho Authorization recebido
  3. Reescreve o caminho para /openai/v1/chat/completions
  4. Obtém um token para https://ai.azure.com
                 |
                 v
Azure OpenAI
  POST https://<recurso>.openai.azure.com/openai/v1/chat/completions
  Authorization: Bearer <token-da-identidade-gerenciada>
```

O nome do deployment permanece no campo `model` do corpo JSON. A API v1 não exige `api-version` na query string.

## Seleção temporária de backend no chat

A operação existente `POST /openai/v1/chat/completions` usa [`samples/apim-unified-chat-policy.xml`](../samples/apim-unified-chat-policy.xml) durante a migração gradual. O corpo público permanece compatível com `OpenAI.chat.completions`; `X-API-Mode` seleciona o backend:

| Seletor | Backend | Caminho | Audiência Entra |
| --- | --- | --- | --- |
| ausente ou `v1` | `<recurso>.openai.azure.com` | `/openai/v1/chat/completions` | `https://ai.azure.com` |
| `legacy` | `<recurso>.openai.azure.com` | `/openai/deployments/<deployment>/chat/completions?api-version=2024-10-21` | `https://cognitiveservices.azure.com` |

A policy remove `X-API-Mode` e `Authorization` antes de encaminhar a requisição. Somente um valor não vazio diferente de `v1|legacy` retorna `400` com o código `invalid_api_mode`. Header ausente preserva clientes v1 existentes; falhas de backend nunca causam fallback entre modos. A seleção dual cobre somente chat e deve ser removida ao final da migração.

## Política completa

```xml
<policies>
    <inbound>
        <base />
        <rate-limit-by-key calls="60" renewal-period="60" counter-key="@(context.Subscription?.Key ?? context.Request.IpAddress)" />
        <set-header name="Authorization" exists-action="delete" />
        <rewrite-uri template="__BACKEND_PATH__" copy-unmatched-params="false" />
        <authentication-managed-identity resource="https://ai.azure.com" client-id="__APIM_IDENTITY_CLIENT_ID__" />
    </inbound>
    <backend>
        <retry condition="@(context.Response != null &amp;&amp; (context.Response.StatusCode == 429 || context.Response.StatusCode &gt;= 500))" count="2" interval="1" delta="1" max-interval="4" first-fast-retry="false">
          <forward-request buffer-request-body="true" />
        </retry>
    </backend>
    <outbound>
        <base />
    </outbound>
    <on-error>
        <base />
    </on-error>
</policies>
```

## Explicação dos elementos

### `policies`

É o elemento raiz. Ele organiza as políticas pelas quatro fases do processamento no APIM: entrada, backend, saída e erro.

### `inbound`

Executa antes de o APIM encaminhar a requisição ao Azure OpenAI. As decisões específicas desta POC estão nesta fase.

### `base`

Herda as políticas definidas no escopo superior, como produto, API, workspace ou instância do APIM. A posição de `<base />` importa: neste exemplo, as políticas herdadas são executadas antes das regras locais.

Revise as políticas herdadas para garantir que nenhuma delas:

- Adicione uma query string `api-version` fora do branch legado da fachada.
- Reescreva a URL para `/models` ou para `/openai/deployments` fora do branch legado da fachada.
- Substitua o backend configurado para a API.
- Registre o corpo, o token ou chaves em logs.

### Remoção do cabeçalho `Authorization`

```xml
<set-header name="Authorization" exists-action="delete" />
```

O construtor `OpenAI` exige um valor em `api_key` e cria um cabeçalho `Authorization: Bearer ...`. No fluxo da POC, esse valor autentica apenas o formato esperado pelo cliente e não deve chegar ao Azure OpenAI.

A política remove o cabeçalho recebido antes de executar a autenticação por identidade gerenciada. Isso evita encaminhar uma credencial inadequada ou permitir que o cliente escolha a identidade usada pelo APIM no backend.

### Telemetria sanitizada

A policy emite um trace `openai-v1-migration` com somente o identificador da requisição e uma dimensão `api_mode` limitada a `default-v1`, `v1`, `legacy` ou `invalid`. O diagnóstico do APIM captura zero bytes de corpo e nenhum header no frontend ou backend. Não adicione prompts, respostas, tokens, chaves ou valores livres do cliente a esse trace.

### Limite, retry e reescrita

Cada assinatura/IP recebe 60 chamadas por minuto. O gateway repete no máximo duas vezes somente respostas `5xx`, com espera crescente limitada a quatro segundos. Respostas `429` voltam ao SDK, que respeita a política de retry configurada sem multiplicá-la pelo retry do gateway. Ajuste esses valores a partir de carga medida, quota do deployment e tolerância de latência.

```xml
<rewrite-uri template="__BACKEND_PATH__" copy-unmatched-params="false" />
```

A instrução fixa o caminho enviado ao backend. O backend da API deve apontar para a raiz do recurso:

```text
https://<recurso>.openai.azure.com
```

O resultado enviado pelo APIM será:

```text
https://<recurso>.openai.azure.com/openai/v1/chat/completions
```

`copy-unmatched-params="false"` impede que parâmetros de query não declarados na operação sejam copiados. Essa configuração ajuda a bloquear a propagação acidental de um `api-version` legado. Se a operação precisar aceitar parâmetros de query funcionais, modele-os explicitamente e reavalie esta opção.

O Bicep substitui `__BACKEND_PATH__` pelo caminho correspondente de cada operação. Não use um caminho fixo como política global de uma API que exponha múltiplos endpoints.

### Autenticação por identidade gerenciada

```xml
<authentication-managed-identity resource="https://ai.azure.com" client-id="__APIM_IDENTITY_CLIENT_ID__" />
```

O APIM solicita um token Microsoft Entra usando a identidade gerenciada atribuída pelo usuário e adiciona o token ao cabeçalho `Authorization` da chamada ao backend. Durante o deployment, o Bicep substitui `__APIM_IDENTITY_CLIENT_ID__` pelo client ID real da identidade.

O valor de `resource` é a audiência do token. Para a OpenAI API v1, use:

```text
https://ai.azure.com
```

Não use a audiência legada `https://cognitiveservices.azure.com` nas operações v1. A fachada temporária usa essa audiência somente no branch `legacy` da API Azure OpenAI versionada.

### `backend`, `outbound` e `on-error`

Cada seção contém apenas `<base />`, portanto preserva o comportamento herdado:

| Seção | Função |
| --- | --- |
| `backend` | Controla como a chamada ao backend é executada, incluindo retry ou encaminhamento. |
| `outbound` | Processa a resposta antes de devolvê-la ao cliente. |
| `on-error` | Executa quando ocorre uma falha no pipeline de políticas. |

Na POC, evite adicionar logs com corpo da requisição, corpo da resposta, chaves ou tokens. Status HTTP, duração, request ID e nome da operação são evidências suficientes.

## Pré-requisitos no Azure

Antes de publicar a política:

1. Habilite a identidade gerenciada system-assigned ou user-assigned no APIM.
2. Atribua à identidade o papel `Cognitive Services User` no recurso de Azure OpenAI ou no escopo mínimo aplicável.
3. Configure o backend da API como `https://<recurso>.openai.azure.com`.
4. Configure uma operação que receba `POST /chat/completions` sob o sufixo de API `openai/v1`, ou adapte a URL pública e o rewrite de forma consistente.
5. Exija uma assinatura APIM ou outro mecanismo de autenticação do cliente.
6. Confirme que políticas herdadas não restauram o cabeçalho removido nem alteram a rota.

A atribuição RBAC pode levar alguns minutos para ser propagada.

## Exemplo de requisição

O cliente envia:

```http
POST /openai/v1/chat/completions HTTP/1.1
Host: <apim>.azure-api.net
Ocp-Apim-Subscription-Key: <chave-apim>
Content-Type: application/json

{
  "model": "<nome-do-deployment>",
  "messages": [
    {
      "role": "user",
      "content": "Responda somente: POC SDK OpenAI v1 OK"
    }
  ],
  "max_tokens": 40,
  "temperature": 0
}
```

O APIM encaminha o mesmo corpo para o Azure OpenAI. Somente a URL e a autenticação do backend são alteradas.

## Validação antes da publicação

O validador da POC lê a configuração sem modificá-la:

```powershell
python .\validate_apim.py `
  --resource-group '<resource-group>' `
  --service-name '<apim-name>' `
  --api-id '<api-id>' `
  --export '.\apim-snapshot.json'
```

Revise o resultado e confirme:

- Presença de `/openai/v1` e `chat/completions`.
- Ausência de `/models`, `/openai/deployments` e `api-version` fora da policy unificada de chat.
- Uso da audiência `https://ai.azure.com`.
- Na fachada, presença das duas rotas, duas audiências, seletor explícito e erro `invalid_api_mode`.
- Backend apontando para o recurso correto.
- Ausência de segredos no snapshot exportado.

Depois de publicar a política em um ambiente de POC, execute:

```powershell
$env:APIM_OPENAI_BASE_URL = 'https://<apim>.azure-api.net/openai/v1/'
$env:APIM_SUBSCRIPTION_KEY = '<subscription-key>'
$env:AZURE_OPENAI_DEPLOYMENT = '<nome-do-deployment>'
python .\smoke_test.py --target apim
```

Valide também os dois backends na mesma operação:

```powershell
python .\smoke_test.py --api-mode v1 --target apim
python .\smoke_test.py --api-mode legacy --target apim
python .\load_test.py --target apim --api-mode both --requests 20 --concurrency 4
```

O load test executa os modos em sequência, aplica `--requests` a cada modo e mantém percentis, tokens, throughput, falhas e custo estimado separados por branch.

## Diagnóstico de falhas

| Sintoma | Causa provável | Verificação |
| --- | --- | --- |
| `401` no gateway | Chave APIM ausente ou inválida | Confirme o header `Ocp-Apim-Subscription-Key` e a associação da assinatura ao produto/API. |
| `401` no backend | Token não foi emitido ou o header foi sobrescrito | Inspecione o trace do APIM e a ordem das políticas herdadas. |
| `403` no backend | Identidade sem RBAC ou RBAC ainda não propagado | Confirme a identidade usada e o papel `Cognitive Services User` no recurso correto. |
| `404` | Backend URL, deployment, sufixo da API ou rewrite incorreto | Confirme `/openai/v1/chat/completions` no v1 e `/openai/deployments/<deployment>/chat/completions?api-version=2024-10-21` no legado. |
| `400` com deployment inválido | Campo `model` não corresponde a um deployment | Use o nome do deployment, não o nome comercial do modelo. |
| Erro relacionado a `api-version` | Valor incorreto ou parâmetro propagado fora do branch legado | Remova a query string do v1 e fixe `2024-10-21` somente no rewrite legado. |
| Chamada chega a outro endpoint | Política aplicada no escopo errado | Confirme API, operação e revisão onde a política foi publicada. |

## Segurança e operação

- Não armazene a chave do Azure OpenAI no cliente quando o APIM usa identidade gerenciada.
- Não registre `Authorization`, `Ocp-Apim-Subscription-Key`, prompts ou respostas.
- Restrinja a assinatura APIM ao produto e às APIs necessárias.
- Aplique rate limit e quota conforme o perfil de consumo da aplicação.
- Use revisões do APIM para testar e promover a política.
- Mantenha uma revisão anterior ativa para rollback rápido.

## Rollback

Se a política v1 causar regressão:

1. Reative a revisão anterior da API no APIM.
2. Restaure o base URL anterior no cliente somente durante a janela de contingência.
3. Preserve traces e request IDs da falha sem registrar conteúdo sensível.
4. Corrija a política em uma nova revisão e repita o validador e o smoke test.

O rollback é temporário porque o Azure AI Inference SDK e a rota `/models` estão no caminho de descontinuação descrito no aviso de migração.

## Referências

- [Política de identidade gerenciada do APIM](https://learn.microsoft.com/azure/api-management/authentication-managed-identity-policy)
- [Autenticação e autorização do Azure OpenAI no APIM](https://learn.microsoft.com/azure/api-management/api-management-authenticate-authorize-azure-openai)
- [Migração para o OpenAI SDK e API v1](https://learn.microsoft.com/azure/foundry/how-to/model-inference-to-openai-migration)
