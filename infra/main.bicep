targetScope = 'subscription'

@minLength(1)
@description('Azure Developer CLI environment name.')
param environmentName string

@description('Azure region for all resources.')
param location string

@description('Azure region for API Management. Can differ from the primary resource location during regional recovery.')
param apimLocation string = location

@description('Publisher email required by API Management.')
param publisherEmail string

@description('Publisher organization shown by API Management.')
param publisherName string = 'OpenAI SDK migration POC'

@description('Microsoft Entra object ID granted Monitoring Reader on Application Insights. Leave empty to skip the assignment.')
param telemetryReaderPrincipalId string = ''

@description('Deploy APIM into a VNet and disable public access to Azure OpenAI.')
param enablePrivateNetworking bool = false

@description('Resource ID of the dedicated APIM subnet. Required when private networking is enabled.')
param apimSubnetResourceId string = ''

@description('Resource ID of the subnet used by the Azure OpenAI private endpoint. Required when private networking is enabled.')
param privateEndpointSubnetResourceId string = ''

@description('Resource ID of the VNet linked to privatelink.openai.azure.com. Required when private networking is enabled.')
param virtualNetworkResourceId string = ''

var resourceGroupName = 'rg-${environmentName}'
var resourceToken = uniqueString(subscription().id, location, environmentName)
var apimResourceToken = uniqueString(subscription().id, apimLocation, environmentName)

resource resourceGroup 'Microsoft.Resources/resourceGroups@2024-11-01' = {
  name: resourceGroupName
  location: location
  tags: {
    'azd-env-name': environmentName
  }
}

module resources 'resources.bicep' = {
  name: 'resources-${resourceToken}'
  scope: resourceGroup
  params: {
    apimLocation: apimLocation
    apimResourceToken: apimResourceToken
    location: location
    publisherEmail: publisherEmail
    publisherName: publisherName
    resourceToken: resourceToken
    telemetryReaderPrincipalId: telemetryReaderPrincipalId
    enablePrivateNetworking: enablePrivateNetworking
    apimSubnetResourceId: apimSubnetResourceId
    privateEndpointSubnetResourceId: privateEndpointSubnetResourceId
    virtualNetworkResourceId: virtualNetworkResourceId
  }
}

output RESOURCE_GROUP_ID string = resourceGroup.id
output AZURE_LOCATION string = location
output APIM_LOCATION string = apimLocation
output AZURE_OPENAI_ACCOUNT_NAME string = resources.outputs.azureOpenAIAccountName
output AZURE_OPENAI_BASE_URL string = resources.outputs.azureOpenAIBaseUrl
output AZURE_OPENAI_DEPLOYMENT string = resources.outputs.azureOpenAIDeploymentName
output APIM_SERVICE_NAME string = resources.outputs.apimServiceName
output APIM_API_ID string = resources.outputs.apimApiId
output APIM_OPENAI_BASE_URL string = resources.outputs.apimOpenAIBaseUrl
output APIM_SUBSCRIPTION_ID string = resources.outputs.apimSubscriptionId
output APPLICATION_INSIGHTS_NAME string = resources.outputs.applicationInsightsName
output LOG_ANALYTICS_WORKSPACE_NAME string = resources.outputs.logAnalyticsWorkspaceName
