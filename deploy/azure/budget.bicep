targetScope = 'subscription'
param email string
@minValue(1)
param monthlyLimit int = 15
@description('First day of current month, ISO date e.g. 2026-11-01')
param startDate string
resource budget 'Microsoft.Consumption/budgets@2023-11-01' = {
  name: 'tradex-student-monthly'
  properties: {
    category: 'Cost'
    amount: monthlyLimit
    timeGrain: 'Monthly'
    timePeriod: {startDate: '${startDate}T00:00:00Z'}
    notifications: {
      half: {enabled: true, operator: 'GreaterThanOrEqualTo', threshold: 50, contactEmails: [email], thresholdType: 'Actual'}
      near: {enabled: true, operator: 'GreaterThanOrEqualTo', threshold: 80, contactEmails: [email], thresholdType: 'Actual'}
      full: {enabled: true, operator: 'GreaterThanOrEqualTo', threshold: 100, contactEmails: [email], thresholdType: 'Forecasted'}
    }
  }
}
