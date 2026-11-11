targetScope = 'resourceGroup'

param location string = resourceGroup().location
param vmName string = 'tradex-worker'
param prefix string = 'tradex'

var timeZone = 'India Standard Time'
var weekDays = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday']
var apiVersion = '2024-03-01'
var vmContributorRole = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  '9980e02c-c2be-4d73-94e8-173b1dc7cf3c'
)

resource vm 'Microsoft.Compute/virtualMachines@2024-03-01' existing = {
  name: vmName
}

resource startWorkflow 'Microsoft.Logic/workflows@2019-05-01' = {
  name: '${prefix}-vm-start-weekdays'
  location: location
  identity: { type: 'SystemAssigned' }
  tags: {
    workload: 'TradeX'
    purpose: 'weekday-vm-power-schedule'
    timezone: 'Asia/Kolkata'
  }
  properties: {
    state: 'Enabled'
    definition: {
      '$schema': 'https://schema.management.azure.com/providers/Microsoft.Logic/schemas/2016-06-01/workflowdefinition.json#'
      contentVersion: '1.0.0.0'
      parameters: {}
      triggers: {
        Weekday_0730_IST: {
          type: 'Recurrence'
          recurrence: {
            frequency: 'Week'
            interval: 1
            startTime: '2026-01-05T07:30:00'
            timeZone: timeZone
            schedule: {
              weekDays: weekDays
              hours: [7]
              minutes: [30]
            }
          }
          runtimeConfiguration: { concurrency: { runs: 1 } }
        }
      }
      actions: {
        Start_VM: {
          type: 'Http'
          inputs: {
            method: 'POST'
            uri: 'https://management.azure.com${vm.id}/start?api-version=${apiVersion}'
            authentication: {
              type: 'ManagedServiceIdentity'
              audience: 'https://management.azure.com/'
            }
          }
          runtimeConfiguration: {
            secureData: { properties: ['inputs', 'outputs'] }
          }
        }
      }
      outputs: {}
    }
  }
}

resource stopWorkflow 'Microsoft.Logic/workflows@2019-05-01' = {
  name: '${prefix}-vm-deallocate-weekdays'
  location: location
  identity: { type: 'SystemAssigned' }
  tags: {
    workload: 'TradeX'
    purpose: 'weekday-vm-power-schedule'
    timezone: 'Asia/Kolkata'
  }
  properties: {
    state: 'Enabled'
    definition: {
      '$schema': 'https://schema.management.azure.com/providers/Microsoft.Logic/schemas/2016-06-01/workflowdefinition.json#'
      contentVersion: '1.0.0.0'
      parameters: {}
      triggers: {
        Weekday_1700_IST: {
          type: 'Recurrence'
          recurrence: {
            frequency: 'Week'
            interval: 1
            startTime: '2026-01-05T17:00:00'
            timeZone: timeZone
            schedule: {
              weekDays: weekDays
              hours: [17]
              minutes: [0]
            }
          }
          runtimeConfiguration: { concurrency: { runs: 1 } }
        }
      }
      actions: {
        Deallocate_VM: {
          type: 'Http'
          inputs: {
            method: 'POST'
            uri: 'https://management.azure.com${vm.id}/deallocate?api-version=${apiVersion}'
            authentication: {
              type: 'ManagedServiceIdentity'
              audience: 'https://management.azure.com/'
            }
          }
          runtimeConfiguration: {
            secureData: { properties: ['inputs', 'outputs'] }
          }
        }
      }
      outputs: {}
    }
  }
}

resource startRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(vm.id, startWorkflow.id, 'vm-power')
  scope: vm
  properties: {
    principalId: startWorkflow.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: vmContributorRole
  }
}

resource stopRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(vm.id, stopWorkflow.id, 'vm-power')
  scope: vm
  properties: {
    principalId: stopWorkflow.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: vmContributorRole
  }
}

output startWorkflowName string = startWorkflow.name
output stopWorkflowName string = stopWorkflow.name
output vmId string = vm.id
output schedule string = 'Monday-Friday 07:30 start / 17:00 deallocate Asia/Kolkata'
