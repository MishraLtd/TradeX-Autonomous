"""Public Microsoft retail quotes, not student eligibility or a billing forecast."""
from datetime import datetime, timezone
import json
import sys
from urllib.parse import urlencode
from urllib.request import urlopen

region = sys.argv[1] if len(sys.argv) > 1 else 'centralindia'
sizes = ['Standard_B1s', 'Standard_B2ts_v2', 'Standard_B2ls_v2', 'Standard_B2s', 'Standard_B2s_v2', 'Standard_D2s_v5']
query = "serviceName eq 'Virtual Machines' and armRegionName eq '" + region + "' and priceType eq 'Consumption' and (" + ' or '.join("armSkuName eq '" + s + "'" for s in sizes) + ')'
url = 'https://prices.azure.com/api/retail/prices?' + urlencode({'$filter': query})
rows = []
while url:
    with urlopen(url, timeout=30) as response:
        data = json.load(response)
    for item in data['Items']:
        if (item['productName'].startswith('Virtual Machines ') and 'Windows' not in item['productName']
                and not any(word in item['meterName'] for word in ('Spot', 'Low Priority'))
                and item['unitOfMeasure'] == '1 Hour'):
            row = {k: item[k] for k in ('armSkuName', 'retailPrice', 'currencyCode', 'productName', 'meterName', 'effectiveStartDate', 'meterId')}
            row['monthly_compute_730h'] = round(item['retailPrice'] * 730, 2)
            rows.append(row)
    url = data.get('NextPageLink')
print(json.dumps(dict(checked_at=datetime.now(timezone.utc).isoformat(), region=region,
                     source='https://prices.azure.com/api/retail/prices', compute_only=True, quotes=rows), indent=2))
