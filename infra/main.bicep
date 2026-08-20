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

@description('Azure OpenAI model name.')
param modelName string = 'gpt-4.1-mini'

@description('Azure OpenAI model version.')
param modelVersion string = '2025-04-14'

@description('Azure OpenAI deployment SKU.')
param modelDeploymentSku string = 'GlobalStandard'

@minValue(1)
@description('Azure OpenAI deployment capacity.')
param modelDeploymentCapacity int = 10

@description('API Management SKU.')
param apimSkuName string = 'Developer'

@minValue(0)
@description('API Management capacity. Consumption uses zero; dedicated SKUs use one or more units.')
param apimCapacity int = 1

@minValue(1)
@description('Maximum requests per subscription or caller IP in each 60-second window.')
param rateLimitCallsPerMinute int = 60

@minValue(0)
@description('Number of APIM retries for transient 5xx backend responses.')
param backendRetryCount int = 2

@minValue(1)
@description('Initial delay in seconds between APIM 5xx retries.')
param backendRetryIntervalSeconds int = 1

@minValue(0)
@maxValue(100)
@description('Percentage of APIM request telemetry sampled into Application Insights.')
param telemetrySamplingPercentage int = 100

@minValue(0)
@description('Failed APIM requests in five minutes required to trigger the alert.')
param failedRequestsAlertThreshold int = 5

@description('Alert recipient. Defaults to the APIM publisher email when empty.')
param alertEmail string = ''

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
var validatedApimSubnetResourceId = !enablePrivateNetworking || !empty(apimSubnetResourceId) ? apimSubnetResourceId : fail('apimSubnetResourceId is required when enablePrivateNetworking is true.')
var validatedPrivateEndpointSubnetResourceId = !enablePrivateNetworking || !empty(privateEndpointSubnetResourceId) ? privateEndpointSubnetResourceId : fail('privateEndpointSubnetResourceId is required when enablePrivateNetworking is true.')
var validatedVirtualNetworkResourceId = !enablePrivateNetworking || !empty(virtualNetworkResourceId) ? virtualNetworkResourceId : fail('virtualNetworkResourceId is required when enablePrivateNetworking is true.')

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
    modelName: modelName
    modelVersion: modelVersion
    modelDeploymentSku: modelDeploymentSku
    modelDeploymentCapacity: modelDeploymentCapacity
    apimSkuName: apimSkuName
    apimCapacity: apimCapacity
    rateLimitCallsPerMinute: rateLimitCallsPerMinute
    backendRetryCount: backendRetryCount
    backendRetryIntervalSeconds: backendRetryIntervalSeconds
    telemetrySamplingPercentage: telemetrySamplingPercentage
    failedRequestsAlertThreshold: failedRequestsAlertThreshold
    alertEmail: empty(alertEmail) ? publisherEmail : alertEmail
    resourceToken: resourceToken
    telemetryReaderPrincipalId: telemetryReaderPrincipalId
    enablePrivateNetworking: enablePrivateNetworking
    apimSubnetResourceId: validatedApimSubnetResourceId
    privateEndpointSubnetResourceId: validatedPrivateEndpointSubnetResourceId
    virtualNetworkResourceId: validatedVirtualNetworkResourceId
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
