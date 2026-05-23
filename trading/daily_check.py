"""
Daily morning check - runs at 9:00am ET (9:00pm Singapore time)
Confirms IBKR is connected and logs account status
"""
from ib_insync import *
from dotenv import load_dotenv
from datetime import datetime
import os

load_dotenv()

ib = IB()
ib.connect(
    host=os.getenv('IB_HOST'),
    port=int(os.getenv('IB_PORT')),
    clientId=2
)

if ib.isConnected():
    now = datetime.now().strftime('%Y-%m-%d %H:%M')
    print(f'[{now}] IBKR connected. Account: {ib.managedAccounts()}')
    print(f'[{now}] Prometheus daily check: OK')
else:
    print('ERROR: Could not connect to IBKR')

ib.disconnect()
