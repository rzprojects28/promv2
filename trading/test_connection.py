from ib_insync import *
from dotenv import load_dotenv
import os

load_dotenv()

def test_account(label, port, client_id):
    ib = IB()
    try:
        ib.connect(host='127.0.0.1', port=port, clientId=client_id)
        print(f'\n{"=" * 40}')
        print(f'  SUCCESS: {label}')
        print(f'{"=" * 40}')
        print(f'  Account: {ib.managedAccounts()}')
        print(f'  Connected: {ib.isConnected()}')
        print(f'{"=" * 40}')
        ib.disconnect()
    except Exception as e:
        print(f'\nFAILED ({label}): {e}')

test_account('Account A — Paper (Baseline)', port=4002, client_id=1)
