@description('Azure region for the Microsoft Foundry account.')
param location string = resourceGroup().location

@description('Globally unique name for the Microsoft Foundry account.')
param accountName string

@description('Deployment name exposed to API clients.')
param deploymentName string = 'DeepSeek-V4-Flash'

@description('DeepSeek model name from the regional model catalog.')
param modelName string = 'DeepSeek-V4-Flash'

@description('DeepSeek model version from the regional model catalog.')
param modelVersion string = '2026-04-23'

@description('Azure OpenAI deployment SKU.')
param skuName string = 'GlobalStandard'

@description('Deployment capacity in quota units.')
@minValue(1)
param capacity int = 1

resource foundryAccount 'Microsoft.CognitiveServices/accounts@2025-06-01' = {
  name: accountName
  location: location
  kind: 'AIServices'
  identity: {
    type: 'SystemAssigned'
  }
  sku: {
    name: 'S0'
  }
  properties: {
    allowProjectManagement: true
    customSubDomainName: accountName
    disableLocalAuth: true
    publicNetworkAccess: 'Enabled'
    networkAcls: {
      defaultAction: 'Allow'
    }
  }
}

resource deepSeekDeployment 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = {
  parent: foundryAccount
  name: deploymentName
  sku: {
    name: skuName
    capacity: capacity
  }
  properties: {
    model: {
      format: 'DeepSeek'
      name: modelName
      version: modelVersion
    }
    raiPolicyName: 'Microsoft.DefaultV2'
    versionUpgradeOption: 'NoAutoUpgrade'
  }
}

output deploymentResourceId string = deepSeekDeployment.id
output foundryAccountEndpoint string = foundryAccount.properties.endpoint
output foundryAccountName string = foundryAccount.name
output foundryAccountOpenAIBaseUrl string = '${foundryAccount.properties.endpoint}openai/v1'
output deploymentName string = deepSeekDeployment.name
