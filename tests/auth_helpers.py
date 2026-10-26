from unittest.mock import patch
from server import accounts


def authorize_owner(test, client, root):
    for change in [patch.object(accounts, 'DATABASE_PATH', root / 'customers.sqlite3'),
                   patch.object(accounts, 'CUSTOMER_ROOT', root / 'customers'),
                   patch.object(accounts, 'AUTH_LIMITER', accounts.auth_limiter())]:
        change.start()
        test.addCleanup(change.stop)
    accounts.initialize()
    customer_id = accounts.create_customer('Test Owner', 'owner@example.test', 'test-only-owner-password', role='admin', status='approved')
    client.cookies.set(accounts.COOKIE, accounts.new_session(customer_id))
    return client
