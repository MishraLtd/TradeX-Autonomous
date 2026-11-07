targetScope = 'resourceGroup'
param location string = resourceGroup().location
param prefix string = 'tradex'
@allowed(['Standard_B1s', 'Standard_B2ts_v2', 'Standard_B2ls_v2', 'Standard_B2s', 'Standard_B2s_v2', 'Standard_D2s_v5'])
param vmSize string = 'Standard_B2ts_v2'
param adminUser string = 'tradexadmin'
param sshPublicKey string
@description('One administrator public IPv4 CIDR, e.g. 203.0.113.1/32. Never use * or 0.0.0.0/0.')
@minLength(9)
param adminCidr string
var suffix = uniqueString(resourceGroup().id)
resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: 'tx${suffix}'
  location: location
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: {
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
  }
}
resource blobs 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storage
  name: 'default'
  properties: {
    deleteRetentionPolicy: { enabled: true, days: 14 }
    isVersioningEnabled: true
  }
}
resource container 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobs
  name: 'operations'
  properties: { publicAccess: 'None' }
}
resource retention 'Microsoft.Storage/storageAccounts/managementPolicies@2023-05-01' = {
  parent: storage
  name: 'default'
  properties: {policy: {rules: [
    {
      name: 'backup-retention'
      enabled: true
      type: 'Lifecycle'
      definition: {
        filters: {blobTypes: ['blockBlob'], prefixMatch: ['operations/backups/']}
        actions: {
          baseBlob: {delete: {daysAfterModificationGreaterThan: 30}}
          version: {delete: {daysAfterCreationGreaterThan: 7}}
        }
      }
    }
    {
      name: 'health-version-retention'
      enabled: true
      type: 'Lifecycle'
      definition: {
        filters: {blobTypes: ['blockBlob'], prefixMatch: ['operations/health.json']}
        actions: {version: {delete: {daysAfterCreationGreaterThan: 1}}}
      }
    }
  ]}}
}
resource nsg 'Microsoft.Network/networkSecurityGroups@2023-09-01' = {
  name: '${prefix}-nsg'
  location: location
  properties: {
    securityRules: [{
      name: 'AdminSSH'
      properties: {
        priority: 100
        access: 'Allow'
        direction: 'Inbound'
        protocol: 'Tcp'
        sourceAddressPrefix: adminCidr
        sourcePortRange: '*'
        destinationAddressPrefix: '*'
        destinationPortRange: '22'
      }
    }]
  }
}
resource vnet 'Microsoft.Network/virtualNetworks@2023-09-01' = {
  name: '${prefix}-vnet'
  location: location
  properties: {
    addressSpace: { addressPrefixes: ['10.34.0.0/16'] }
    subnets: [{name: 'worker', properties: {addressPrefix: '10.34.1.0/24', networkSecurityGroup: {id: nsg.id}}}]
  }
}
resource ip 'Microsoft.Network/publicIPAddresses@2023-09-01' = {
  name: '${prefix}-ip'
  location: location
  sku: {name: 'Standard'}
  properties: {publicIPAllocationMethod: 'Static'}
}
resource nic 'Microsoft.Network/networkInterfaces@2023-09-01' = {
  name: '${prefix}-nic'
  location: location
  properties: {
    ipConfigurations: [{name: 'primary', properties: {
      privateIPAllocationMethod: 'Dynamic'
      subnet: {id: '${vnet.id}/subnets/worker'}
      publicIPAddress: {id: ip.id}
    }}]
  }
}
resource vm 'Microsoft.Compute/virtualMachines@2024-03-01' = {
  name: '${prefix}-worker'
  location: location
  identity: {type: 'SystemAssigned'}
  properties: {
    hardwareProfile: {vmSize: vmSize}
    osProfile: {
      computerName: '${prefix}-worker'
      adminUsername: adminUser
      linuxConfiguration: {
        disablePasswordAuthentication: true
        ssh: {publicKeys: [{path: '/home/${adminUser}/.ssh/authorized_keys', keyData: sshPublicKey}]}
      }
    }
    storageProfile: {
      imageReference: {publisher: 'Canonical', offer: 'ubuntu-24_04-lts', sku: 'server', version: 'latest'}
      osDisk: {createOption: 'FromImage', deleteOption: 'Detach', diskSizeGB: 32, managedDisk: {storageAccountType: 'StandardSSD_LRS'}}
    }
    networkProfile: {networkInterfaces: [{id: nic.id}]}
    diagnosticsProfile: {bootDiagnostics: {enabled: true}}
  }
}
resource blobRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, vm.id, 'blob-contributor')
  scope: storage
  properties: {
    principalId: vm.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'ba92f5b4-2d11-453d-a403-e96b0029c9fe')
  }
}
resource vault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: 'tx-${suffix}'
  location: location
  properties: {
    tenantId: tenant().tenantId
    sku: {family: 'A', name: 'standard'}
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 14
    accessPolicies: []
  }
}
resource secretRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(vault.id, vm.id, 'secret-reader')
  scope: vault
  properties: {
    principalId: vm.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '4633458b-17de-408a-b874-0445c86b69e6')
  }
}
output workerIp string = ip.properties.ipAddress
output storageName string = storage.name
output vaultName string = vault.name
output identityId string = vm.identity.principalId
