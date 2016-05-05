#    Copyright 2016 Mirantis, Inc.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import logging
from multiprocessing.dummy import Pool

import dpath.util
from novaclient import exceptions as nova_exceptions
import pytest
from six.moves import configparser

from mos_tests.functions import common

logger = logging.getLogger(__name__)
pytestmark = pytest.mark.undestructive


def is_migrated(os_conn, instances, target=None, source=None):
    assert any([source, target]), 'One of target or source is required'
    for instance in instances:
        instance.get()
        host = getattr(instance, 'OS-EXT-SRV-ATTR:host')
        if not os_conn.is_server_active(instance):
            return False
        if target and host != target:
            return False
        if source and host == source:
            return False
    return True


@pytest.yield_fixture(scope='module')
def unlimited_live_migrations(env):
    nova_config_path = '/etc/nova/nova.conf'

    nodes = (
        env.get_nodes_by_role('controller') + env.get_nodes_by_role('compute'))
    for node in nodes:
        if 'controller' in node.data['roles']:
            restart_cmd = 'service nova-api restart'
        else:
            restart_cmd = 'service nova-compute restart'
        with node.ssh() as remote:
            remote.check_call('cp {0} {0}.bak'.format(nova_config_path))

            parser = configparser.RawConfigParser()
            with remote.open(nova_config_path) as f:
                parser.readfp(f)
            parser.set('DEFAULT', 'max_concurrent_live_migrations', 0)
            with remote.open(nova_config_path, 'w') as f:
                parser.write(f)
            remote.check_call(restart_cmd)

    common.wait(env.os_conn.is_nova_ready,
                timeout_seconds=60 * 5,
                expected_exceptions=Exception,
                waiting_for="Nova services to be alive")

    yield
    for node in nodes:
        if 'controller' in node.data['roles']:
            restart_cmd = 'service nova-api restart'
        else:
            restart_cmd = 'service nova-compute restart'
        with node.ssh() as remote:
            result = remote.execute('mv {0}.bak {0}'.format(nova_config_path))
            if result.is_ok:
                remote.check_call(restart_cmd)

    common.wait(env.os_conn.is_nova_ready,
                timeout_seconds=60 * 5,
                expected_exceptions=Exception,
                waiting_for="Nova services to be alive")


@pytest.fixture
def big_hypervisors(os_conn):
    hypervisors = os_conn.nova.hypervisors.list()
    for flavor in os_conn.nova.flavors.list():
        suitable_hypervisors = []
        for hypervisor in hypervisors:
            if os_conn.get_hypervisor_capacity(hypervisor, flavor) > 0:
                suitable_hypervisors.append(hypervisor)
        hypervisors = suitable_hypervisors
    if len(hypervisors) < 2:
        pytest.skip('This test requires minimum 2 hypervisors '
                    'suitable for max flavor')
    return hypervisors[:2]


@pytest.yield_fixture
def big_port_quota(os_conn):
    tenant = os_conn.neutron.get_quotas_tenant()
    tenant_id = tenant['tenant']['tenant_id']
    orig_quota = os_conn.neutron.show_quota(tenant_id)
    new_quota = orig_quota.copy()
    # update quota for class C net
    new_quota['quota']['port'] = 256
    os_conn.neutron.update_quota(tenant_id, new_quota)
    yield
    os_conn.neutron.update_quota(tenant_id, orig_quota)


@pytest.fixture(scope='session')
def block_migration(env, request):
    value = request.param
    data = env.get_settings_data()
    if dpath.util.get(data, '*/storage/**/ephemeral_ceph/value') and value:
        pytest.skip('Block migration requires Nova Ceph RBD to be disabled')
    if not dpath.util.get(data,
                          '*/storage/**/ephemeral_ceph/value') and not value:
        pytest.skip('True live migration requires Nova Ceph RBD')
    return value


class TestLiveMigration(object):
    @pytest.fixture(autouse=True)
    def init(self, env, os_conn, keypair, security_group, network):
        self.env = env
        self.os_conn = os_conn
        self.keypair = keypair
        self.security_group = security_group
        self.network = network
        self.instances = []
        self.volumes = []

    def create_instances(self, zone, flavor, instances_count):
        logger.info('Start with flavor {0.name}, '
                    'creates {1} instances'.format(flavor, instances_count))
        for i in range(instances_count):
            instance = self.os_conn.create_server(
                name='server%02d' % i,
                flavor=flavor,
                availability_zone=zone,
                key_name=self.keypair.name,
                nics=[{'net-id': self.network['network']['id']}],
                security_groups=[self.security_group.id],
                wait_for_active=False,
                wait_for_avaliable=False)
            self.instances.append(instance)
        common.wait(
            lambda: all(self.os_conn.is_server_active(x)
                        for x in self.instances),
            timeout_seconds=3 * 60,
            waiting_for='instances to became to ACTIVE status'
        )  # yapf: disable
        common.wait(
            lambda: all(self.os_conn.is_server_ssh_ready(x)
                        for x in self.instances),
            timeout_seconds=3 * 60,
            waiting_for='instances to be ssh available')  # yapf: disable

    def delete_instances(self, force=False):
        for instance in self.instances:
            if force:
                delete = instance.force_delete
            else:
                delete = instance.delete
            try:
                delete()
            except nova_exceptions.NotFound:
                pass
        common.wait(
            lambda: all(self.os_conn.is_server_deleted(x.id)
                        for x in self.instances),
            timeout_seconds=2 * 60,
            waiting_for='instances to be deleted')  # yapf: disable

    @pytest.yield_fixture
    def cleanup_instances(self):
        yield
        self.delete_instances(force=True)

    @pytest.yield_fixture
    def cleanup_volumes(self):
        yield
        for volume in self.volumes:
            common.delete_volume(self.os_conn.cinder, volume)

    def successive_migration(self, block_migration, hypervisor_from):
        logger.info('Start successive migrations')
        for instance in self.instances:
            instance.live_migrate(block_migration=block_migration)

        common.wait(
            lambda: is_migrated(self.os_conn, self.instances,
                                source=hypervisor_from.hypervisor_hostname),
            timeout_seconds=5 * 60,
            waiting_for='instances to migrate from '
                        '{0.hypervisor_hostname}'.format(hypervisor_from))
        hyp1_id = hypervisor_from.id
        common.wait(
            lambda: (
                self.os_conn.nova.hypervisors.get(hyp1_id).running_vms == 0),
            timeout_seconds=2 * 60,
            waiting_for='hypervisor info be updated')  # yapf: disable

    def concurrent_migration(self, block_migration, hypervisor_to):
        pool = Pool(len(self.instances))
        logger.info('Start concurrent migrations')
        host = hypervisor_to.hypervisor_hostname
        try:
            pool.map(
                lambda x: x.live_migrate(host=host,
                                         block_migration=block_migration),
                self.instances)
        finally:
            pool.terminate()

        common.wait(
            lambda: is_migrated(self.os_conn, self.instances,
                                target=hypervisor_to.hypervisor_hostname),
            timeout_seconds=5 * 60,
            waiting_for='instances to migrate to '
                        '{0.hypervisor_hostname}'.format(hypervisor_to))

    @pytest.mark.testrail_id('838028', block_migration=True)
    @pytest.mark.testrail_id('838257', block_migration=False)
    @pytest.mark.parametrize('block_migration',
                             [True, False],
                             ids=['block LM', 'true LM'],
                             indirect=True)
    @pytest.mark.usefixtures('unlimited_live_migrations', 'cleanup_instances')
    def test_live_migration_max_instances_with_all_flavors(
            self, big_hypervisors, block_migration, big_port_quota):
        """LM of maximum allowed amount of instances created with all available
            flavors

        Scenario:
            1. Allow unlimited concurrent live migrations
            2. Restart nova-api services on controllers and
                nova-compute services on computes
            3. Create maximum allowed number of instances on a single
                compute node
            4. Initiate serial block LM of previously created instances
                to another compute node and estimate total time elapsed
            5. Check that all live-migrated instances are hosted on target host
                and are in Active state:
            6. Send pings between pairs of VMs to check that network
                connectivity between these hosts is still alive
            7. Initiate concurrent block LM of previously created instances
                to another compute node and estimate total time elapsed
            8. Check that all live-migrated instances are hosted on target host
                and are in Active state
            9. Send pings between pairs of VMs to check that network
                connectivity between these hosts is alive
            10. Repeat pp.3-9 for every available flavor
        """
        zone = self.os_conn.nova.availability_zones.find(zoneName="nova")
        hypervisor1, hypervisor2 = big_hypervisors
        flavors = sorted(self.os_conn.nova.flavors.list(),
                         key=lambda x: -x.ram)
        for flavor in flavors:
            # Skip small flavors
            if flavor.ram < 512:
                continue
            instances_count = min(
                self.os_conn.get_hypervisor_capacity(hypervisor1, flavor),
                self.os_conn.get_hypervisor_capacity(hypervisor2, flavor))

            instance_zone = '{}:{}'.format(zone.zoneName,
                                           hypervisor1.hypervisor_hostname)
            self.create_instances(instance_zone, flavor, instances_count)

            self.successive_migration(block_migration,
                                      hypervisor_from=hypervisor1)

            common.wait(
                lambda: all(self.os_conn.is_server_ssh_ready(x)
                            for x in self.instances),
                timeout_seconds=3 * 60,
                waiting_for='instances to be ssh available')  # yapf: disable

            self.concurrent_migration(block_migration,
                                     hypervisor_to=hypervisor1)

            common.wait(
                lambda: all(self.os_conn.is_server_ssh_ready(x)
                            for x in self.instances),
                timeout_seconds=3 * 60,
                waiting_for='instances to be ssh available')  # yapf: disable

            self.delete_instances()

    @pytest.mark.testrail_id('838029', block_migration=True)
    @pytest.mark.testrail_id('838258', block_migration=False)
    @pytest.mark.usefixtures('unlimited_live_migrations', 'cleanup_instances',
                             'cleanup_volumes')
    @pytest.mark.parametrize('block_migration',
                             [True, False],
                             ids=['block LM', 'true LM'],
                             indirect=True)
    def test_live_migration_with_volumes(self, big_hypervisors,
                                         block_migration):
        """LM of instances with volumes attached

        Scenario:
            1. Allow unlimited concurrent live migrations
            2. Restart nova-api services on controllers and
                nova-compute services on computes
            3. Create maximum allowed number of instances with attached volumes
                on a single compute node
            4. Initiate serial block LM of previously created instances
                to another compute node
            5. Check that all live-migrated instances are hosted on target host
                and are in Active state:
            6. Send pings between pairs of VMs to check that network
                connectivity between these hosts is still alive
            7. Initiate concurrent block LM of previously created instances
                to another compute node
            8. Check that all live-migrated instances are hosted on target host
                and are in Active state
            9. Send pings between pairs of VMs to check that network
                connectivity between these hosts is alive
        """
        zone = self.os_conn.nova.availability_zones.find(zoneName="nova")
        hypervisor1, hypervisor2 = big_hypervisors
        flavor = sorted(self.os_conn.nova.flavors.list(),
                        key=lambda x: x.ram)[0]
        project_id = self.os_conn.session.get_project_id()
        max_volumes = self.os_conn.cinder.quotas.get(project_id).volumes
        instances_count = min(
            self.os_conn.get_hypervisor_capacity(hypervisor1, flavor),
            self.os_conn.get_hypervisor_capacity(hypervisor2, flavor),
            max_volumes)
        instance_zone = '{}:{}'.format(zone.zoneName,
                                       hypervisor1.hypervisor_hostname)
        self.create_instances(instance_zone, flavor, instances_count)

        image = self.os_conn._get_cirros_image()

        for instance in self.instances:
            vol = common.create_volume(self.os_conn.cinder,
                                       image['id'],
                                       size=1,
                                       timeout=5,
                                       name='{0.name}_volume'.format(instance))
            self.volumes.append(vol)
            self.os_conn.nova.volumes.create_server_volume(instance.id, vol.id)

        self.successive_migration(block_migration, hypervisor_from=hypervisor1)

        common.wait(
            lambda: all(self.os_conn.is_server_ssh_ready(x)
                        for x in self.instances),
            timeout_seconds=3 * 60,
            waiting_for='instances to be ssh available')  # yapf: disable

        self.concurrent_migration(block_migration, hypervisor_to=hypervisor1)

        common.wait(
            lambda: all(self.os_conn.is_server_ssh_ready(x)
                        for x in self.instances),
            timeout_seconds=3 * 60,
            waiting_for='instances to be ssh available')  # yapf: disable

        self.delete_instances()