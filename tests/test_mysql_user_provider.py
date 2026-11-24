"""
Runs the providers against a real MySQL 8.0 server with an in-memory Parameter Store:

    docker run -d --name cfn-mysql-test -e MYSQL_ROOT_PASSWORD=password -p 8033:3306 mysql:8.0
    AWS_DEFAULT_REGION=us-east-1 PYTHONPATH=src pytest tests

IAM authentication (AWSAuthenticationPlugin) only exists on RDS, so it isn't covered here.
"""
import os
import uuid

import mysql.connector
import pytest
from botocore.exceptions import ClientError

import mysql_user_provider
from mysql_user_provider import handler

HOST = os.environ.get('MYSQL_TEST_HOST', '127.0.0.1')
PORT = int(os.environ.get('MYSQL_TEST_PORT', '8033'))
ROOT_PASSWORD = os.environ.get('MYSQL_TEST_ROOT_PASSWORD', 'password')
ADMIN_PARAMETER = '/platform/aurora/test/database-admin-password'


class FakeSSM:
    def __init__(self):
        self.parameters = {}
        self.puts = []

    def get_parameter(self, Name, WithDecryption=False):
        if Name not in self.parameters:
            raise ClientError({'Error': {'Code': 'ParameterNotFound', 'Message': Name}}, 'GetParameter')
        return {'Parameter': {'Name': Name, 'Value': self.parameters[Name]}}

    def put_parameter(self, Name, Value, **kwargs):
        self.parameters[Name] = Value
        self.puts.append(Name)


@pytest.fixture(autouse=True)
def ssm(monkeypatch):
    fake = FakeSSM()
    fake.parameters[ADMIN_PARAMETER] = ROOT_PASSWORD
    monkeypatch.setattr(mysql_user_provider.provider, 'ssm', fake)
    monkeypatch.setattr(mysql_user_provider.database_provider, 'ssm', fake)
    # responses go to a CloudFormation pre-signed URL; the tests read the returned response instead
    monkeypatch.setattr('cfn_resource_provider.resource_provider.requests.put',
                        lambda *a, **k: type('Response', (), {'status_code': 200})())
    return fake


def root(database=None):
    return mysql.connector.connect(host=HOST, port=PORT, user='root', password=ROOT_PASSWORD, database=database)


def query(sql, params=()):
    connection = root()
    try:
        cursor = connection.cursor()
        cursor.execute(sql, params)
        return cursor.fetchall()
    finally:
        connection.close()


def can_login(user, password):
    try:
        mysql.connector.connect(host=HOST, port=PORT, user=user, password=password).close()
        return True
    except mysql.connector.Error:
        return False


def unique(prefix='u'):
    return '%s%s' % (prefix, uuid.uuid4().hex[:12])


def connection(dbname='mysql'):
    return {'Host': HOST, 'Port': PORT, 'DBName': dbname, 'User': 'root', 'PasswordParameterName': ADMIN_PARAMETER}


def event(request_type, properties, resource_type='Custom::MySQLUser', physical_resource_id=None,
          old_properties=None):
    request = {
        'RequestType': request_type,
        'ResponseURL': 'https://cloudformation-response.invalid/',
        'StackId': 'arn:aws:cloudformation:us-east-1:123456789012:stack/test/guid',
        'RequestId': str(uuid.uuid4()),
        'ResourceType': resource_type,
        'LogicalResourceId': 'Test',
        'ResourceProperties': dict(properties),
    }
    if physical_resource_id:
        request['PhysicalResourceId'] = physical_resource_id
    if old_properties is not None:
        request['OldResourceProperties'] = dict(old_properties)
    return request


def ok(response):
    assert response['Status'] == 'SUCCESS', response['Reason']
    return response


@pytest.fixture
def database():
    name = unique('db')
    query('CREATE DATABASE `%s`' % name)
    yield name
    query('DROP DATABASE IF EXISTS `%s`' % name)


def password_user(name, database, **extra):
    properties = {'User': name, 'PasswordParameterName': '/application/parameters/test/%s/password' % name,
                  'WithDatabase': 'false', 'Database': connection(database)}
    properties.update(extra)
    return properties


def test_generates_and_stores_a_password_when_the_parameter_is_missing(ssm, database):
    properties = password_user(unique(), database)
    ok(handler(event('Create', properties), {}))

    stored = ssm.parameters[properties['PasswordParameterName']]
    assert can_login(properties['User'], stored)


def test_uses_an_existing_password_and_keeps_it_on_update(ssm, database):
    properties = password_user(unique(), database, UserPermissions=['SELECT'])
    ssm.parameters[properties['PasswordParameterName']] = 'set-by-pco-env'

    created = ok(handler(event('Create', properties), {}))
    updated_properties = dict(properties, UserPermissions=['SELECT', 'INSERT'])
    ok(handler(event('Update', updated_properties, physical_resource_id=created['PhysicalResourceId'],
                     old_properties=properties), {}))

    assert ssm.puts == []
    assert ssm.parameters[properties['PasswordParameterName']] == 'set-by-pco-env'
    assert can_login(properties['User'], 'set-by-pco-env')


def test_creating_over_an_existing_user_keeps_its_password(ssm, database):
    name = unique()
    query("CREATE USER %s@'%%' IDENTIFIED BY 'already-in-use'", (name,))
    properties = password_user(name, database)
    ssm.parameters[properties['PasswordParameterName']] = 'already-in-use'

    ok(handler(event('Create', properties), {}))

    assert ssm.puts == []
    assert can_login(name, 'already-in-use')


def test_rotation_applies_the_parameter_changed_outside_the_provider(ssm, database):
    # rotating = `pco env update` the parameter, then any update of the resource
    properties = password_user(unique(), database)
    ssm.parameters[properties['PasswordParameterName']] = 'original'
    created = ok(handler(event('Create', properties), {}))

    ssm.parameters[properties['PasswordParameterName']] = 'rotated-by-pco-env'
    bumped = dict(properties, PasswordVersion='2')
    ok(handler(event('Update', bumped, physical_resource_id=created['PhysicalResourceId'],
                     old_properties=properties), {}))

    assert ssm.puts == []
    assert can_login(properties['User'], 'rotated-by-pco-env')
    assert not can_login(properties['User'], 'original')


def test_physical_ids_match_the_format_existing_resources_have(ssm, database):
    # Update refuses to run when the computed id differs from the stored one,
    # so this format must not change under resources already deployed.
    name = unique()
    created = ok(handler(event('Create', password_user(name, database)), {}))
    assert created['PhysicalResourceId'] == 'mysql:%s:%d:%s::%s' % (HOST, PORT, database, name)


def test_host_scoped_user_can_be_updated_and_locked(ssm, database):
    name = unique()
    properties = password_user('%s@10.%%' % name, database, UserPermissions=['SELECT', 'SHOW VIEW'],
                               GlobalUserPermissions=['SHOW DATABASES'])
    properties['PasswordParameterName'] = '/application/parameters/all/production/mysql/test-shared-password'
    ssm.parameters[properties['PasswordParameterName']] = 'shared'

    created = ok(handler(event('Create', properties), {}))
    ok(handler(event('Update', properties, physical_resource_id=created['PhysicalResourceId'],
                     old_properties=properties), {}))

    grants = ' '.join(row[0] for row in query("SHOW GRANTS FOR %s@%s", (name, '10.%')))
    assert 'SHOW DATABASES ON *.*' in grants
    assert 'SELECT, SHOW VIEW ON `%s`.*' % database in grants
    assert ssm.puts == []

    ok(handler(event('Delete', properties, physical_resource_id=created['PhysicalResourceId']), {}))
    assert query("SELECT account_locked FROM mysql.user WHERE user = %s AND host = %s", (name, '10.%')) == [('Y',)]


def test_database_is_created_once_and_never_dropped(ssm):
    name = unique('db')
    properties = {'DatabaseName': name, 'Database': connection()}
    try:
        created = ok(handler(event('Create', properties, 'Custom::MySQLDatabase'), {}))
        assert created['PhysicalResourceId'] == 'mysql-database:%s:%d:%s' % (HOST, PORT, name)
        assert query('SELECT default_character_set_name FROM information_schema.schemata WHERE schema_name = %s',
                     (name,)) == [('utf8mb4',)]

        # creating again is a no-op, not an error
        ok(handler(event('Create', properties, 'Custom::MySQLDatabase'), {}))

        ok(handler(event('Delete', properties, 'Custom::MySQLDatabase',
                         physical_resource_id=created['PhysicalResourceId']), {}))
        assert query('SELECT 1 FROM information_schema.schemata WHERE schema_name = %s', (name,)) == [(1,)]
    finally:
        query('DROP DATABASE IF EXISTS `%s`' % name)


def test_renaming_a_database_creates_the_new_one_and_keeps_the_old(ssm):
    old, new = unique('db'), unique('db')
    try:
        created = ok(handler(event('Create', {'DatabaseName': old, 'Database': connection()},
                                   'Custom::MySQLDatabase'), {}))
        updated = ok(handler(event('Update', {'DatabaseName': new, 'Database': connection()},
                                   'Custom::MySQLDatabase', physical_resource_id=created['PhysicalResourceId'],
                                   old_properties={'DatabaseName': old, 'Database': connection()}), {}))
        assert updated['PhysicalResourceId'].endswith(':' + new)
        names = [row[0] for row in query(
            'SELECT schema_name FROM information_schema.schemata WHERE schema_name IN (%s, %s)', (old, new))]
        assert sorted(names) == sorted([old, new])
    finally:
        query('DROP DATABASE IF EXISTS `%s`' % old)
        query('DROP DATABASE IF EXISTS `%s`' % new)


def test_rejects_a_database_name_with_a_backtick(ssm):
    response = handler(event('Create', {'DatabaseName': 'x`; DROP DATABASE mysql; --', 'Database': connection()},
                             'Custom::MySQLDatabase'), {})
    assert response['Status'] == 'FAILED'
    assert response['Reason'].startswith('invalid resource properties'), response['Reason']


class FakeConfigTable:
    def __init__(self):
        self.items = {}
        self.role_arns = []

    def get_item(self, TableName, Key):
        item = self.items.get((Key['PK']['S'], Key['SK']['S']))
        return {'Item': item} if item else {}


@pytest.fixture
def config_table(monkeypatch):
    fake = FakeConfigTable()

    def assume(role_arn):
        fake.role_arns.append(role_arn)
        return fake
    monkeypatch.setattr(mysql_user_provider.provider, 'config_table', assume)
    return fake


def config_user(name, database):
    properties = password_user(name, database)
    del properties['PasswordParameterName']
    properties['PasswordPCOConfig'] = {'RoleArn': 'arn:aws:iam::499739914416:role/pco-config-read',
                                    'TableName': 'pco-config-table', 'Namespace': 'screens',
                                    'Name': 'production/database/password'}
    return properties


def test_reads_an_existing_password_from_pco_config(ssm, config_table, database):
    properties = config_user(unique(), database)
    config_table.items[('P#screens', 'LATEST#GLOBAL#production/database/password')] = {
        'Value': {'S': 'set-in-pco-config'}}

    created = ok(handler(event('Create', properties), {}))
    ok(handler(event('Update', properties, physical_resource_id=created['PhysicalResourceId'],
                     old_properties=properties), {}))

    assert can_login(properties['User'], 'set-in-pco-config')
    assert config_table.role_arns[0] == properties['PasswordPCOConfig']['RoleArn']
    assert ssm.puts == []


@pytest.mark.parametrize('config_name', ['sentry/dsn', 'production/database/password_backup', 'database/passwords'])
def test_only_reads_database_passwords_from_pco_config(ssm, config_table, database, config_name):
    properties = config_user(unique(), database)
    properties['PasswordPCOConfig']['Name'] = config_name
    config_table.items[('P#screens', 'LATEST#GLOBAL#' + config_name)] = {'Value': {'S': 'another-apps-secret'}}

    response = handler(event('Create', properties), {})

    assert response['Status'] == 'FAILED'
    assert 'can only read a database password' in response['Reason']
    assert config_table.role_arns == []


@pytest.mark.parametrize('item', [None, {'Value': {'S': 'DELETED'}, 'DeletedAt': {'S': '2026-11-23T00:00:00Z'}},
                                  {'Value': {'S': 'PROMOTED UPDATE ME'}}])
def test_fails_when_the_pco_config_password_is_missing_deleted_or_a_placeholder(ssm, config_table, database, item):
    properties = config_user(unique(), database)
    if item:
        config_table.items[('P#screens', 'LATEST#GLOBAL#production/database/password')] = item

    response = handler(event('Create', properties), {})

    assert response['Status'] == 'FAILED'
    assert 'create it there first' in response['Reason']
    assert query("SELECT 1 FROM mysql.user WHERE user = %s", (properties['User'],)) == []
    assert ssm.puts == []
