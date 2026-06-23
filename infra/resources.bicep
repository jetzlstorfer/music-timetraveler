@description('Name of the azd environment used for stable per-environment resource names.')
param environmentName string

@description('The location for all resources.')
param location string

@description('Tags to apply to all resources.')
param tags object

@description('Unique resource token derived from the subscription, environment name, and location.')
@minLength(2)
param resourceToken string

@description('Last.fm API key stored as a Container App secret.')
@secure()
param lastfmApiKey string = ''

@description('Spotify OAuth client id (https://developer.spotify.com/dashboard).')
param spotifyClientId string = ''

@description('Spotify OAuth client secret stored as a Container App secret.')
@secure()
param spotifyClientSecret string = ''

@description('Spotify OAuth redirect URI. Must match a value registered in the Spotify app dashboard.')
param spotifyRedirectUri string = ''

@description('Fernet key (32-byte url-safe base64) used to encrypt Spotify refresh tokens at rest.')
@secure()
param spotifyTokenEncryptionKey string = ''

@description('Custom domain hostname to bind to the Container App (e.g. musictimetraveler.jetzlstorfer.org). Leave empty to skip custom domain binding.')
param customDomainName string = ''

@description('Resource ID of the managed certificate in the Container Apps Environment for the custom domain. Leave empty to skip custom domain binding.')
param customDomainCertificateId string = ''

var normalizedEnvironmentName = toLower(replace(replace(environmentName, '_', '-'), ' ', '-'))
var compactEnvironmentName = take(toLower(replace(replace(replace(replace(environmentName, '-', ''), '_', ''), ' ', ''), '.', '')), 41)
var acrName = 'acr${compactEnvironmentName}${take(resourceToken, 6)}'
var logAnalyticsName = take('log-${normalizedEnvironmentName}', 63)
var containerAppsEnvironmentName = take('cae-${normalizedEnvironmentName}', 32)
var containerAppName = take('ca-${normalizedEnvironmentName}', 32)
var cosmosAccountName = take('cosmos-${normalizedEnvironmentName}-${take(resourceToken, 6)}', 50)
var cosmosDatabaseName = 'lastfm-timetraveler'
var cosmosContainerName = 'searches'

// Container Apps rejects secrets with empty values. Only include the Spotify
// secrets/env vars when the caller actually provided them, so `azd up` works
// even before the user registers a Spotify OAuth app. Same for Last.fm: the
// app supports Spotify-only deployments, so an empty LASTFM_API_KEY must not
// fail provisioning.
var spotifyConfigured = !empty(spotifyClientId) && !empty(spotifyClientSecret) && !empty(spotifyRedirectUri) && !empty(spotifyTokenEncryptionKey)
var lastfmConfigured = !empty(lastfmApiKey)
var customDomainConfigured = !empty(customDomainName) && !empty(customDomainCertificateId)

// ---------------------------------------------------------------------------
// Log Analytics Workspace (required by Container Apps Environment)
// ---------------------------------------------------------------------------
resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2022-10-01' = {
  name: logAnalyticsName
  location: location
  tags: tags
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 30
  }
}

// ---------------------------------------------------------------------------
// Azure Container Registry
// ---------------------------------------------------------------------------
resource acr 'Microsoft.ContainerRegistry/registries@2023-01-01-preview' = {
  name: acrName
  location: location
  tags: tags
  sku: {
    name: 'Basic'
  }
  properties: {
    adminUserEnabled: true
  }
}

// ---------------------------------------------------------------------------
// Azure Cosmos DB for NoSQL (serverless cache store)
// ---------------------------------------------------------------------------
resource cosmosAccount 'Microsoft.DocumentDB/databaseAccounts@2024-11-15' = {
  name: cosmosAccountName
  location: location
  kind: 'GlobalDocumentDB'
  tags: tags
  properties: {
    databaseAccountOfferType: 'Standard'
    locations: [
      {
        locationName: location
        failoverPriority: 0
        isZoneRedundant: false
      }
    ]
    capabilities: [
      {
        name: 'EnableServerless'
      }
    ]
    consistencyPolicy: {
      defaultConsistencyLevel: 'Session'
    }
    backupPolicy: {
      type: 'Periodic'
      periodicModeProperties: {
        backupIntervalInMinutes: 240
        backupRetentionIntervalInHours: 8
        backupStorageRedundancy: 'Local'
      }
    }
    publicNetworkAccess: 'Enabled'
    disableLocalAuth: false
    minimalTlsVersion: 'Tls12'
  }
}

resource cosmosDatabase 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases@2024-11-15' = {
  name: cosmosDatabaseName
  parent: cosmosAccount
  properties: {
    resource: {
      id: cosmosDatabaseName
    }
  }
}

resource cosmosContainer 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-11-15' = {
  name: cosmosContainerName
  parent: cosmosDatabase
  properties: {
    resource: {
      id: cosmosContainerName
      partitionKey: {
        paths: [
          '/username_normalized'
        ]
        kind: 'Hash'
        version: 2
      }
    }
  }
}

resource cosmosSpotifyProfilesContainer 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-11-15' = {
  name: 'spotify_profiles'
  parent: cosmosDatabase
  properties: {
    resource: {
      id: 'spotify_profiles'
      partitionKey: {
        paths: [
          '/profile_id_normalized'
        ]
        kind: 'Hash'
        version: 2
      }
      // 90-day TTL: documents auto-expire 90 days after their last write.
      // The app refreshes the profile on every authenticated access so active
      // users never lose their data; abandoned profiles age out automatically.
      defaultTtl: 7776000
    }
  }
}

resource cosmosSpotifyPlaysContainer 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-11-15' = {
  name: 'spotify_plays'
  parent: cosmosDatabase
  properties: {
    resource: {
      id: 'spotify_plays'
      partitionKey: {
        paths: [
          '/profile_id_normalized'
        ]
        kind: 'Hash'
        version: 2
      }
      // 90-day TTL: play documents auto-expire 90 days after their last write.
      // Re-uploading the same Spotify export upserts every play and refreshes
      // its TTL, so users who keep their data fresh keep it indefinitely.
      defaultTtl: 7776000
    }
  }
}

resource cosmosSpotifySessionsContainer 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-11-15' = {
  name: 'spotify_sessions'
  parent: cosmosDatabase
  properties: {
    resource: {
      id: 'spotify_sessions'
      partitionKey: {
        paths: [
          '/session_id_hash'
        ]
        kind: 'Hash'
        version: 2
      }
      // 30-day rolling TTL refreshed on each authenticated request.
      defaultTtl: 2592000
    }
  }
}

// ---------------------------------------------------------------------------
// Container Apps Environment
// ---------------------------------------------------------------------------
resource containerAppsEnvironment 'Microsoft.App/managedEnvironments@2023-05-01' = {
  name: containerAppsEnvironmentName
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalytics.properties.customerId
        sharedKey: logAnalytics.listKeys().primarySharedKey
      }
    }
  }
}

// ---------------------------------------------------------------------------
// Container App
// ---------------------------------------------------------------------------
resource containerApp 'Microsoft.App/containerApps@2023-05-01' = {
  name: containerAppName
  location: location
  tags: union(tags, { 'azd-service-name': 'web' })
  dependsOn: [
    cosmosContainer
    cosmosSpotifyProfilesContainer
    cosmosSpotifyPlaysContainer
    cosmosSpotifySessionsContainer
  ]
  properties: {
    managedEnvironmentId: containerAppsEnvironment.id
    configuration: {
      ingress: {
        external: true
        targetPort: 5000
        transport: 'auto'
        customDomains: customDomainConfigured ? [
          {
            name: customDomainName
            bindingType: 'SniEnabled'
            certificateId: customDomainCertificateId
          }
        ] : []
      }
      registries: [
        {
          server: acr.properties.loginServer
          username: acr.listCredentials().username
          passwordSecretRef: 'acr-password'
        }
      ]
      secrets: concat([
        {
          name: 'acr-password'
          value: acr.listCredentials().passwords[0].value
        }
        {
          name: 'cosmos-connection-string'
          value: cosmosAccount.listConnectionStrings().connectionStrings[0].connectionString
        }
      ], lastfmConfigured ? [
        {
          name: 'lastfm-api-key'
          value: lastfmApiKey
        }
      ] : [], spotifyConfigured ? [
        {
          name: 'spotify-client-secret'
          value: spotifyClientSecret
        }
        {
          name: 'spotify-token-encryption-key'
          value: spotifyTokenEncryptionKey
        }
      ] : [])
    }
    template: {
      containers: [
        {
          name: 'web'
          // azd replaces this placeholder with the image it builds and pushes to ACR during `azd deploy`.
          image: 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest'
          env: concat([
            {
              name: 'COSMOS_CONNECTION_STRING'
              secretRef: 'cosmos-connection-string'
            }
            {
              name: 'COSMOS_DATABASE_NAME'
              value: cosmosDatabaseName
            }
            {
              name: 'COSMOS_CONTAINER_NAME'
              value: cosmosContainerName
            }
          ], lastfmConfigured ? [
            {
              name: 'LASTFM_API_KEY'
              secretRef: 'lastfm-api-key'
            }
          ] : [], spotifyConfigured ? [
            {
              name: 'SPOTIFY_CLIENT_ID'
              value: spotifyClientId
            }
            {
              name: 'SPOTIFY_CLIENT_SECRET'
              secretRef: 'spotify-client-secret'
            }
            {
              name: 'SPOTIFY_REDIRECT_URI'
              value: spotifyRedirectUri
            }
            {
              name: 'SPOTIFY_TOKEN_ENCRYPTION_KEY'
              secretRef: 'spotify-token-encryption-key'
            }
          ] : [])
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
          probes: [
            {
              type: 'Startup'
              httpGet: {
                path: '/api/ready'
                port: 5000
              }
              initialDelaySeconds: 10
              periodSeconds: 5
              timeoutSeconds: 10
              failureThreshold: 24
            }
            {
              type: 'Readiness'
              httpGet: {
                path: '/api/ready'
                port: 5000
              }
              initialDelaySeconds: 10
              periodSeconds: 10
              timeoutSeconds: 10
              failureThreshold: 3
            }
            {
              type: 'Liveness'
              httpGet: {
                path: '/api/status'
                port: 5000
              }
              initialDelaySeconds: 30
              periodSeconds: 30
              timeoutSeconds: 3
              failureThreshold: 3
            }
          ]
        }
      ]
      scale: {
        // Set to 0 to minimise cost; increase to 1 if cold-start latency is unacceptable.
        minReplicas: 0
        maxReplicas: 10
      }
    }
  }
}

output AZURE_CONTAINER_REGISTRY_ENDPOINT string = acr.properties.loginServer
output AZURE_CONTAINER_REGISTRY_NAME string = acr.name
output SERVICE_WEB_URI string = 'https://${containerApp.properties.configuration.ingress.fqdn}'
output AZURE_COSMOS_DB_ACCOUNT_NAME string = cosmosAccount.name
output AZURE_COSMOS_DB_ENDPOINT string = cosmosAccount.properties.documentEndpoint
