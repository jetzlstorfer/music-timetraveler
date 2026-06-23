targetScope = 'subscription'

@minLength(1)
@maxLength(64)
@description('Name of the environment that is used to generate a short unique hash for resources.')
param environmentName string

@minLength(1)
@description('Primary location for all resources.')
param location string

@description('Last.fm API key.')
@secure()
param lastfmApiKey string = ''

@description('Spotify OAuth client id.')
param spotifyClientId string = ''

@description('Spotify OAuth client secret.')
@secure()
param spotifyClientSecret string = ''

@description('Spotify OAuth redirect URI (must match the Spotify app dashboard).')
param spotifyRedirectUri string = ''

@description('Fernet key (url-safe base64) used to encrypt Spotify refresh tokens.')
@secure()
param spotifyTokenEncryptionKey string = ''

@description('Custom domain hostname to bind to the Container App (e.g. musictimetraveler.jetzlstorfer.org). Leave empty to skip.')
param customDomainName string = ''

@description('Resource ID of the managed certificate in the Container Apps Environment for the custom domain. Leave empty to skip.')
param customDomainCertificateId string = ''

var tags = { 'azd-env-name': environmentName }
var resourceToken = toLower(uniqueString(subscription().id, environmentName, location))

resource rg 'Microsoft.Resources/resourceGroups@2022-09-01' = {
  name: 'rg-${environmentName}'
  location: location
  tags: tags
}

module resources 'resources.bicep' = {
  name: 'resources'
  scope: rg
  params: {
    environmentName: environmentName
    location: location
    tags: tags
    resourceToken: resourceToken
    lastfmApiKey: lastfmApiKey
    spotifyClientId: spotifyClientId
    spotifyClientSecret: spotifyClientSecret
    spotifyRedirectUri: spotifyRedirectUri
    spotifyTokenEncryptionKey: spotifyTokenEncryptionKey
    customDomainName: customDomainName
    customDomainCertificateId: customDomainCertificateId
  }
}

output AZURE_CONTAINER_REGISTRY_ENDPOINT string = resources.outputs.AZURE_CONTAINER_REGISTRY_ENDPOINT
output AZURE_CONTAINER_REGISTRY_NAME string = resources.outputs.AZURE_CONTAINER_REGISTRY_NAME
output SERVICE_WEB_URI string = resources.outputs.SERVICE_WEB_URI
output AZURE_COSMOS_DB_ACCOUNT_NAME string = resources.outputs.AZURE_COSMOS_DB_ACCOUNT_NAME
output AZURE_COSMOS_DB_ENDPOINT string = resources.outputs.AZURE_COSMOS_DB_ENDPOINT
