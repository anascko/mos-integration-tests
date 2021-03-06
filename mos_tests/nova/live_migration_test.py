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

from collections import defaultdict
import logging
from multiprocessing.dummy import Pool

import dpath.util
from novaclient import exceptions as nova_exceptions
import pytest
from six.moves import configparser

from mos_tests.conftest import ubuntu_image_id as ubuntu_image_id_base
from mos_tests.functions import common
from mos_tests.functions import service

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


@pytest.yield_fixture(scope='module', autouse=True)
def disable_nova_config_drive(get_env):
    # WA for bug https://bugs.launchpad.net/mos/+bug/1589460/
    # This should be removed in MOS 10.0
    env = get_env()
    config = [('DEFAULT', 'force_config_drive', False)]
    for step in service.nova_patch(env, config):
        yield step


@pytest.yield_fixture
def cleanup_virsh_domains(env, os_conn):
    # WA for bug https://bugs.launchpad.net/mos/+bug/1591676/
    # destroy all created during test virsh domains
    exists = defaultdict(set)
    for instance in os_conn.nova.servers.list():
        host = getattr(instance, 'OS-EXT-SRV-ATTR:hypervisor_hostname')
        exists[host].add(getattr(instance, 'OS-EXT-SRV-ATTR:instance_name'))
    yield
    for node in env.get_nodes_by_role('compute'):
        with node.ssh() as remote:
            result = remote.execute("virsh list | grep running | "
                                    "awk '{ print $2 }'")
            if not result.is_ok:
                continue
            vms = set(result.stdout_string.split())
            new_vms = vms - exists.get(node.data['fqdn'], set())
            for vm in new_vms:
                remote.execute('virsh destroy {0}'.format(vm))


@pytest.yield_fixture(scope='module')
def unlimited_live_migrations(get_env):
    env = get_env()
    config = [('DEFAULT', 'max_concurrent_live_migrations', 0)]
    for step in service.nova_patch(env, config):
        yield step


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
def block_migration(get_env, request):
    env = get_env()
    value = request.param
    data = env.get_settings_data()
    if dpath.util.get(data, '*/storage/**/ephemeral_ceph/value') and value:
        pytest.skip('Block migration requires Nova Ceph RBD to be disabled')
    if not dpath.util.get(data,
                          '*/storage/**/ephemeral_ceph/value') and not value:
        pytest.skip('True live migration requires Nova Ceph RBD')
    return value


@pytest.yield_fixture(scope='module')
def ubuntu_image_id(os_conn):
    for step in ubuntu_image_id_base(os_conn):
        yield step


@pytest.yield_fixture
def router(os_conn, network):
    router = os_conn.create_router(name='router01')
    os_conn.router_gateway_add(router_id=router['router']['id'],
                               network_id=os_conn.ext_network['id'])

    subnet = os_conn.neutron.list_subnets(
        network_id=network['network']['id'])['subnets'][0]

    os_conn.router_interface_add(router_id=router['router']['id'],
                                 subnet_id=subnet['id'])
    yield router
    os_conn.delete_router(router['router']['id'])


@pytest.mark.usefixtures('cleanup_virsh_domains')
class TestLiveMigrationBase(object):
    @pytest.fixture(autouse=True)
    def init(self, env, os_conn, keypair, security_group, network):
        self.env = env
        self.os_conn = os_conn
        self.keypair = keypair
        self.security_group = security_group
        self.network = network
        self.instances = []
        self.volumes = []

    def create_instances(self,
                         zone,
                         flavor,
                         instances_count,
                         image_id=None,
                         userdata=None,
                         create_args=None):
        boot_marker = 'INSTANCE BOOT COMPLETED'

        logger.info('Start with flavor {0.name}, '
                    'creates {1} instances'.format(flavor, instances_count))
        if userdata is not None:
            userdata += '\necho "{marker}"'.format(marker=boot_marker)

        if create_args is not None:
            assert len(create_args) == instances_count
        else:
            create_args = [{}] * instances_count
        for i in range(instances_count):
            kwargs = create_args[i]
            instance = self.os_conn.create_server(
                name='server%02d' % i,
                image_id=image_id,
                userdata=userdata,
                flavor=flavor,
                availability_zone=zone,
                key_name=self.keypair.name,
                nics=[{'net-id': self.network['network']['id']}],
                security_groups=[self.security_group.id],
                wait_for_active=False,
                wait_for_avaliable=False,
                **kwargs)
            self.instances.append(instance)
        self.os_conn.wait_servers_active(self.instances)

        if userdata is None:
            self.os_conn.wait_servers_ssh_ready(self.instances)
        else:
            self.os_conn.wait_marker_in_servers_log(self.instances,
                                                    marker=boot_marker)

    def delete_instances(self):
        hypervisors = set()
        for instance in self.instances:
            hypervisors.add(getattr(instance,
                                    'OS-EXT-SRV-ATTR:hypervisor_hostname'))
            try:
                instance.delete()
            except nova_exceptions.NotFound:
                pass
        common.wait(
            lambda: all(self.os_conn.is_server_deleted(x.id)
                        for x in self.instances),
            timeout_seconds=2 * 60,
            waiting_for='instances to be deleted')
        self.instances = []
        for hypervisor in self.os_conn.nova.hypervisors.list():
            if hypervisor.hypervisor_hostname in hypervisors:
                self.os_conn.wait_hypervisor_be_free(hypervisor)

    @pytest.fixture(scope='session')
    def nova_ceph(self, get_env, request):
        env = get_env()
        data = env.get_settings_data()
        return dpath.util.get(data, '*/storage/**/ephemeral_ceph/value')

    def check_lm_restrictions(self, nova_ceph, volume_backed, block_migration):
        if not nova_ceph and volume_backed == block_migration:
            pytest.skip("Block migration is not allowed with volume backed "
                        "instances")

    def make_stress_instances(self,
                              ubuntu_image_id,
                              instances_count,
                              zone,
                              create_args=None,
                              flavor=None):
        userdata = '\n'.join([
            '#!/bin/bash -v',
            'apt-get install -yq stress cpulimit sysstat iperf',
        ])

        flavor = flavor or self.os_conn.nova.flavors.find(name='m1.small')
        self.create_instances(zone=zone,
                              flavor=flavor,
                              instances_count=instances_count,
                              image_id=ubuntu_image_id,
                              userdata=userdata,
                              create_args=create_args)

    @pytest.fixture
    def stress_instances(self, request, ubuntu_image_id, os_conn, nova_ceph,
                         block_migration, big_hypervisors):
        project_id = os_conn.session.get_project_id()
        max_volumes = os_conn.cinder.quotas.get(project_id).volumes
        params = getattr(request, 'param', {'volume_backed': False,
                                            'inst_count': 'max'})
        self.check_lm_restrictions(nova_ceph, params['volume_backed'],
                                   block_migration)

        hypervisor1, hypervisor2 = big_hypervisors
        flavor = self.os_conn.nova.flavors.find(name='m1.small')

        if 'inst_count' in params.keys():
            if type(params['inst_count']) is int:
                instances_count = params['inst_count']
        else:
            instances_count = min(
                self.os_conn.get_hypervisor_capacity(hypervisor1, flavor),
                self.os_conn.get_hypervisor_capacity(hypervisor2, flavor))
        instances_zone = 'nova:{0.hypervisor_hostname}'.format(hypervisor1)
        create_args = None
        if params['volume_backed']:
            instances_count = min(instances_count, max_volumes)
            create_args = []
            for i in range(instances_count):
                vol = common.create_volume(os_conn.cinder,
                                           image_id=ubuntu_image_id,
                                           size=5)
                self.volumes.append(vol)
                create_args.append(dict(block_device_mapping={'vda': vol.id}))
            request.addfinalizer(lambda: os_conn.delete_volumes(self.volumes))
        self.make_stress_instances(ubuntu_image_id,
                                   instances_count=instances_count,
                                   zone=instances_zone, flavor=flavor,
                                   create_args=create_args)
        request.addfinalizer(lambda: self.delete_instances())
        return self.instances

    @pytest.fixture
    def stress_instance(self, request, os_conn, ubuntu_image_id, nova_ceph,
                        block_migration):
        params = getattr(request, 'param', {'volume_backed': False})
        self.check_lm_restrictions(nova_ceph, params['volume_backed'],
                                   block_migration)
        create_args = None
        if params['volume_backed']:
            vol = common.create_volume(os_conn.cinder,
                                       image_id=ubuntu_image_id, size=5)
            self.volumes.append(vol)
            create_args = [dict(block_device_mapping={'vda': vol.id})]
            request.addfinalizer(lambda: os_conn.delete_volumes(self.volumes))
        self.make_stress_instances(ubuntu_image_id,
                                   instances_count=1,
                                   zone='nova',
                                   create_args=create_args)
        request.addfinalizer(lambda: self.delete_instances())
        instance = self.instances[0]
        return instance

    @pytest.fixture
    def iperf_instances(self, request, os_conn, keypair, security_group,
                        network, ubuntu_image_id, block_migration, nova_ceph):
        params = getattr(request, 'param', {'volume_backed': False})
        self.check_lm_restrictions(nova_ceph, params['volume_backed'],
                                   block_migration)
        userdata = '\n'.join([
            '#!/bin/bash -v',
            'apt-get install -yq iperf',
            'iperf -u -s -p 5002 <&- >/dev/null 2>&1 &',
        ])
        flavor = os_conn.nova.flavors.find(name='m1.small')
        create_args = None
        if params['volume_backed']:
            create_args = []
            for i in range(2):
                vol = common.create_volume(os_conn.cinder, size=5,
                                           image_id=ubuntu_image_id)
                self.volumes.append(vol)
                create_args.append(dict(block_device_mapping={'vda': vol.id}))
            request.addfinalizer(lambda: os_conn.delete_volumes(self.volumes))
        self.create_instances(zone='nova',
                              flavor=flavor,
                              instances_count=2,
                              image_id=ubuntu_image_id,
                              userdata=userdata,
                              create_args=create_args)
        request.addfinalizer(lambda: self.delete_instances())
        return self.instances

    @pytest.yield_fixture
    def cleanup_instances(self):
        yield
        self.delete_instances()

    @pytest.yield_fixture
    def cleanup_volumes(self, os_conn):
        yield
        os_conn.delete_volumes(self.volumes)

    def successive_migration(self, block_migration, hypervisor_from):
        logger.info('Start successive migrations')
        for instance in self.instances:
            instance.live_migrate(block_migration=block_migration)

        common.wait(
            lambda: is_migrated(self.os_conn, self.instances,
                                source=hypervisor_from.hypervisor_hostname),
            timeout_seconds=20 * 60,
            waiting_for='instances to migrate from '
                        '{0.hypervisor_hostname}'.format(hypervisor_from))

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
            timeout_seconds=20 * 60,
            waiting_for='instances to migrate to '
                        '{0.hypervisor_hostname}'.format(hypervisor_to))

    def check_volumes_have_status(self, status):
        assert all(
            map(
                lambda volume: (volume.get(), volume.status == status)[-1],
                self.volumes))


class TestLiveMigrationSomeFlavors(TestLiveMigrationBase):
    @pytest.mark.testrail_id('838028', block_migration=True)
    @pytest.mark.testrail_id('838257',
                             block_migration=False,
                             with_volume=False)
    @pytest.mark.testrail_id('838231', block_migration=False, with_volume=True)
    @pytest.mark.parametrize(
        'block_migration, with_volume',
        [(True, False), (False, False), (False, True)],
        ids=['block LM w/o vol', 'true LM w/o vol', 'true LM w vol'],
        indirect=['block_migration'])
    @pytest.mark.usefixtures('unlimited_live_migrations', 'cleanup_instances',
                             'cleanup_volumes')
    def test_live_migration_max_of_instances(self, big_hypervisors,
                                             block_migration, big_port_quota,
                                             with_volume):
        """LM of maximum allowed amount of instances created with all available
            flavors

        Scenario:
            1. Allow unlimited concurrent live migrations
            2. Restart nova-api services on controllers and
                nova-compute services on computes
            3. Create maximum allowed number of instances on a single
                compute node with biggest flavor
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
            10. Repeat pp.3-9 for smallest available flavor
        """
        project_id = self.os_conn.session.get_project_id()
        image = self.os_conn._get_cirros_image()

        instances_create_args = []
        if with_volume:
            max_volumes = self.os_conn.cinder.quotas.get(project_id).volumes
            for i in range(max_volumes):
                vol = common.create_volume(self.os_conn.cinder,
                                           image['id'],
                                           size=10,
                                           timeout=5,
                                           name='volume_i'.format(i))
                self.volumes.append(vol)
                instances_create_args.append(dict(
                    block_device_mapping={'vda': vol.id}))

        zone = self.os_conn.nova.availability_zones.find(zoneName="nova")
        hypervisor1, hypervisor2 = big_hypervisors
        flavors = sorted(self.os_conn.nova.flavors.list(),
                         key=lambda x: -x.ram)
        # Skip small flavors
        flavors = [x for x in flavors if x.ram >= 512]
        for flavor in flavors[0], flavors[-1]:

            instances_count = min(
                self.os_conn.get_hypervisor_capacity(hypervisor1, flavor),
                self.os_conn.get_hypervisor_capacity(hypervisor2, flavor))

            instance_zone = '{}:{}'.format(zone.zoneName,
                                           hypervisor1.hypervisor_hostname)
            if with_volume:
                instances_count = min(instances_count, max_volumes)
                create_args = instances_create_args[:instances_count]
            else:
                create_args = None
            self.create_instances(instance_zone,
                                  flavor,
                                  instances_count,
                                  create_args=create_args)

            self.successive_migration(block_migration,
                                      hypervisor_from=hypervisor1)

            self.os_conn.wait_servers_ssh_ready(self.instances)

            self.os_conn.wait_hypervisor_be_free(hypervisor1)

            self.concurrent_migration(block_migration,
                                      hypervisor_to=hypervisor1)

            self.os_conn.wait_servers_ssh_ready(self.instances)

            self.os_conn.wait_hypervisor_be_free(hypervisor2)

            self.delete_instances()


class TestLiveMigrationWithVolumes(TestLiveMigrationBase):

    @pytest.fixture
    def instances(self, request, os_conn, block_migration, big_hypervisors,
                  nova_ceph):
        if not nova_ceph and block_migration:
            pytest.skip('Block migration with attached volumes '
                        'are not allowed in 9.0')
        param = {'boot_from_vol': False}
        param.update(getattr(request, 'param', {}))

        image = os_conn._get_cirros_image()
        zone = os_conn.nova.availability_zones.find(zoneName="nova")
        hypervisor1, hypervisor2 = big_hypervisors
        flavor = sorted(os_conn.nova.flavors.list(), key=lambda x: x.ram)[0]
        project_id = os_conn.session.get_project_id()
        max_volumes = os_conn.cinder.quotas.get(project_id).volumes

        create_args = []
        if param['boot_from_vol']:
            max_volumes /= 2
            for i in range(max_volumes):
                vol = common.create_volume(self.os_conn.cinder,
                                           image['id'],
                                           size=10,
                                           timeout=5,
                                           name='boot_volume_{i}'.format(i=i))
                self.volumes.append(vol)
                create_args.append(dict(block_device_mapping={'vda': vol.id}))

            request.addfinalizer(lambda: os_conn.delete_volumes(self.volumes))

        instances_count = min(
            os_conn.get_hypervisor_capacity(hypervisor1, flavor),
            os_conn.get_hypervisor_capacity(hypervisor2, flavor),
            max_volumes)

        if len(create_args) > 0:
            create_args = create_args[:instances_count]
        else:
            create_args = None

        instance_zone = '{}:{}'.format(zone.zoneName,
                                       hypervisor1.hypervisor_hostname)

        self.create_instances(instance_zone,
                              flavor,
                              instances_count,
                              create_args=create_args)

        request.addfinalizer(lambda: self.delete_instances())

        return self.instances

    @pytest.yield_fixture
    def volumes(self, os_conn, instances):
        volumes = []
        image = os_conn._get_cirros_image()
        for instance in self.instances:
            vol = common.create_volume(self.os_conn.cinder,
                                       image['id'],
                                       size=1,
                                       timeout=5,
                                       name='{0.name}_volume'.format(instance))
            volumes.append(vol)
            os_conn.nova.volumes.create_server_volume(instance.id, vol.id)
        yield volumes
        os_conn.delete_volumes(volumes)

    @pytest.mark.testrail_id('838029', block_migration=True)
    @pytest.mark.testrail_id('838258',
                             block_migration=False,
                             instances={'boot_from_vol': False})
    @pytest.mark.testrail_id('838232',
                             block_migration=False,
                             instances={'boot_from_vol': True})
    @pytest.mark.usefixtures('unlimited_live_migrations')
    @pytest.mark.parametrize('block_migration, instances',
                             [
                                 (True, {'boot_from_vol': False}),
                                 (False, {'boot_from_vol': False}),
                                 (False, {'boot_from_vol': True}),
                             ],
                             ids=[
                                 'block LM-boot from img',
                                 'true LM-boot from img',
                                 'true LM-boot from vol'
                             ],
                             indirect=True)
    def test_live_migration_with_volumes(self, instances, volumes,
                                         big_hypervisors, block_migration):
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
            6. Check that all attached volumes are in 'In-Use' state
            7. Send pings between pairs of VMs to check that network
                connectivity between these hosts is still alive
            8. Initiate concurrent block LM of previously created instances
                to another compute node
            9. Check that all live-migrated instances are hosted on target host
                and are in Active state
            10. Send pings between pairs of VMs to check that network
                connectivity between these hosts is alive
            11. Check that all attached volumes are in 'In-Use' state
        """
        hypervisor1, hypervisor2 = big_hypervisors

        self.successive_migration(block_migration, hypervisor_from=hypervisor1)

        self.os_conn.wait_servers_ssh_ready(self.instances)

        self.check_volumes_have_status('in-use')

        self.os_conn.wait_hypervisor_be_free(hypervisor1)

        self.concurrent_migration(block_migration, hypervisor_to=hypervisor1)

        self.os_conn.wait_servers_ssh_ready(self.instances)

        self.check_volumes_have_status('in-use')


class TestLiveMigrationUnderWorkload(TestLiveMigrationBase):

    memory_cmd = 'stress --vm-bytes 5M --vm-keep -m 1 <&- >/dev/null 2>&1 &'
    cpu_cmd = 'cpulimit -l 50 -- gzip -9 </dev/urandom >/dev/null 2>&1 &'
    hdd_cmd = """for i in {1..3}; do
        killall stress
        stress --hdd $i <&- >/dev/null 2>&1 &
        sleep 5
        util=$(iostat -d -x -y 5 1 | grep -m1 '[hsv]d[abc]' | \
               awk '{print $14}')
        echo "util is $util"
        if [[ $(echo $util'>95' | bc) -eq 1 ]]; then break; fi
    done"""

    @pytest.fixture(scope='session')
    def block_migration(self, request, nova_ceph):
        value = request.param
        if nova_ceph and value:
            pytest.skip('Block migration requires Nova CephRBD to be disabled')
        return value

    @pytest.mark.testrail_id('838032', block_migration=True,
                             stress_instance={'volume_backed': False},
                             cmd=memory_cmd)
    @pytest.mark.testrail_id('838261', block_migration=False,
                             stress_instance={'volume_backed': False},
                             cmd=memory_cmd)
    @pytest.mark.testrail_id('838033', block_migration=True,
                             stress_instance={'volume_backed': False},
                             cmd=cpu_cmd)
    @pytest.mark.testrail_id('838262', block_migration=False,
                             stress_instance={'volume_backed': False},
                             cmd=cpu_cmd)
    @pytest.mark.testrail_id('838035', block_migration=True,
                             stress_instance={'volume_backed': False},
                             cmd=hdd_cmd)
    @pytest.mark.testrail_id('838235', block_migration=False,
                             stress_instance={'volume_backed': True},
                             cmd=memory_cmd)
    @pytest.mark.testrail_id('838236', block_migration=False,
                             stress_instance={'volume_backed': True},
                             cmd=cpu_cmd)
    @pytest.mark.parametrize('block_migration, stress_instance, cmd',

                             [
                                 (True, {'volume_backed': False}, memory_cmd),
                                 (False, {'volume_backed': False}, memory_cmd),
                                 (True, {'volume_backed': False}, cpu_cmd),
                                 (False, {'volume_backed': False}, cpu_cmd),
                                 (True, {'volume_backed': False}, hdd_cmd),
                                 (False, {'volume_backed': True}, memory_cmd),
                                 (False, {'volume_backed': True}, cpu_cmd),
                             ],
                             ids=[
                                 'block LM mem',
                                 'true LM mem',
                                 'block LM cpu',
                                 'true LM cpu',
                                 'block LM hdd',
                                 'true volume-backed LM cpu',
                                 'true volume-backed LM hdd',
                             ],
                             indirect=['block_migration', 'stress_instance'])
    @pytest.mark.usefixtures('router')
    def test_lm_with_workload(self, stress_instance, keypair, block_migration,
                              cmd):
        """LM of instance under memory workload

        Scenario:
            1. Boot an instance with Ubuntu image as a source and install
                the some stress utilities on it
            2. Generate a workload with executing command on instance
            3. Initiate live migration to another compute node
            4. Check that instance is hosted on another host and on ACTIVE
                status
            5. Check that network connectivity to instance is OK
        """
        with self.os_conn.ssh_to_instance(self.env,
                                          stress_instance,
                                          vm_keypair=keypair,
                                          username='ubuntu') as remote:
            remote.check_call(cmd)

        old_host = getattr(stress_instance, 'OS-EXT-SRV-ATTR:host')
        stress_instance.live_migrate(block_migration=block_migration)

        common.wait(
            lambda: is_migrated(self.os_conn, [stress_instance],
                                source=old_host),
            timeout_seconds=5 * 60,
            waiting_for='instance to migrate from {0}'.format(old_host))

        common.wait(lambda: self.os_conn.is_server_ssh_ready(stress_instance),
                    timeout_seconds=2 * 60,
                    waiting_for='instance to be available via ssh')

    @pytest.mark.testrail_id('838034', block_migration=True,
                             iperf_instances={'volume_backed': False})
    @pytest.mark.testrail_id('838263', block_migration=False,
                             iperf_instances={'volume_backed': False})
    @pytest.mark.testrail_id('838237', block_migration=False,
                             iperf_instances={'volume_backed': True})
    @pytest.mark.parametrize('block_migration, iperf_instances',
                             [
                                 (True, {'volume_backed': False}),
                                 (False, {'volume_backed': False}),
                                 (False, {'volume_backed': True})
                             ],
                             ids=['block LM', 'true LM', 'true LM for volume'],
                             indirect=['block_migration', 'iperf_instances'])
    @pytest.mark.usefixtures('router')
    def test_lm_with_network_workload(self, iperf_instances, keypair,
                                      block_migration):
        """LM of instance under memory workload

        Scenario:
            1. Boot 2 instances with Ubuntu image as a source and install
                the iperf on it
            2. Start iperf server on first instance:
                iperf -u -s -p 5002
            2. Generate a workload with executing command on second instance:
                iperf --port 5002 -u --client <vm1_fixed_ip> --len 64 \
                --bandwidth 5M --time 60 -i 10
            3. Initiate live migration first instance to another compute node
            4. Check that instance is hosted on another host and on ACTIVE
                status
            5. Check that network connectivity to instance is OK
        """
        client, server = iperf_instances
        server_ip = self.os_conn.get_nova_instance_ips(server)['fixed']
        with self.os_conn.ssh_to_instance(self.env,
                                          client,
                                          vm_keypair=keypair,
                                          username='ubuntu') as remote:
            remote.check_call('iperf -u -c {ip} -p 5002 -t 240 --len 64'
                              '--bandwidth 5M <&- >/dev/null 2&>1 &'.format(
                                  ip=server_ip))

        old_host = getattr(server, 'OS-EXT-SRV-ATTR:host')
        server.live_migrate(block_migration=block_migration)

        common.wait(
            lambda: is_migrated(self.os_conn, [server],
                                source=old_host),
            timeout_seconds=5 * 60,
            waiting_for='instance to migrate from {0}'.format(old_host))

        common.wait(lambda: self.os_conn.is_server_ssh_ready(server),
                    timeout_seconds=2 * 60,
                    waiting_for='instance to be available via ssh')

    @pytest.mark.testrail_id('838037', block_migration=True,
                             stress_instances={'volume_backed': False},
                             cmd=cpu_cmd)
    @pytest.mark.testrail_id('838265', block_migration=False,
                             stress_instances={'volume_backed': False},
                             cmd=cpu_cmd)
    @pytest.mark.testrail_id('838036', block_migration=True,
                             stress_instances={'volume_backed': False},
                             cmd=memory_cmd)
    @pytest.mark.testrail_id('838264', block_migration=False,
                             stress_instances={'volume_backed': False},
                             cmd=memory_cmd)
    @pytest.mark.testrail_id('838239', block_migration=False,
                             stress_instances={'volume_backed': True},
                             cmd=cpu_cmd)
    @pytest.mark.testrail_id('838238', block_migration=False,
                             stress_instances={'volume_backed': True},
                             cmd=memory_cmd)
    @pytest.mark.parametrize('block_migration, stress_instances, cmd',
                             [
                                 (True, {'volume_backed': False}, cpu_cmd),
                                 (False, {'volume_backed': False}, cpu_cmd),
                                 (True, {'volume_backed': False}, memory_cmd),
                                 (False, {'volume_backed': False}, memory_cmd),
                                 (False, {'volume_backed': True}, cpu_cmd),
                                 (False, {'volume_backed': True}, memory_cmd),
                             ],
                             ids=[
                                 'cpu-block LM',
                                 'cpu-true LM',
                                 'memory-block LM',
                                 'memory-true LM',
                                 'cpu-true LM volume-backed',
                                 'memory-true LM volume-backed',
                             ],
                             indirect=['block_migration', 'stress_instances'])
    @pytest.mark.usefixtures('router', 'unlimited_live_migrations')
    def test_lm_under_work_multi_instances(self, stress_instances, keypair,
                                           big_hypervisors, block_migration,
                                           cmd):
        """LM of multiple instances under workload

        Scenario:
            1. Allow unlimited concurrent live migrations
            2. Restart nova-api services on controllers and
                nova-compute services on computes
            3. Create maximum allowed number of instances on a single compute
                node and install stress utilities on it
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
        hypervisor1, _ = big_hypervisors
        for instance in self.instances:
            with self.os_conn.ssh_to_instance(self.env,
                                              instance,
                                              vm_keypair=keypair,
                                              username='ubuntu') as remote:
                remote.check_call(cmd)

        self.successive_migration(block_migration, hypervisor_from=hypervisor1)

        self.os_conn.wait_servers_ssh_ready(self.instances)

        self.os_conn.wait_hypervisor_be_free(hypervisor1)

        self.concurrent_migration(block_migration, hypervisor_to=hypervisor1)

        self.os_conn.wait_servers_ssh_ready(self.instances)

    @pytest.mark.testrail_id('838039')
    @pytest.mark.parametrize('block_migration, stress_instances, cmd',
                             [(True, {'volume_backed': False,
                                      'inst_count': 2},
                               hdd_cmd)],
                             ids=['hdd-block LM, 2 instances'],
                             indirect=['block_migration', 'stress_instances'])
    @pytest.mark.usefixtures('router', 'unlimited_live_migrations')
    def test_lm_under_work_multi_instances_hdd_block(
            self, stress_instances, keypair, big_hypervisors, block_migration,
            cmd):
        """LM of multiple instances under workload

        Scenario:
            1. Allow unlimited concurrent live migrations
            2. Restart nova-api services on controllers and
                nova-compute services on computes
            3. Create maximum allowed number of instances on a single compute
                node and install stress utilities on it
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
        hypervisor1, _ = big_hypervisors
        for instance in self.instances:
            with self.os_conn.ssh_to_instance(self.env,
                                              instance,
                                              vm_keypair=keypair,
                                              username='ubuntu') as remote:
                remote.check_call(cmd)

        self.successive_migration(block_migration, hypervisor_from=hypervisor1)

        self.os_conn.wait_servers_ssh_ready(self.instances)

        self.os_conn.wait_hypervisor_be_free(hypervisor1)

    @pytest.mark.testrail_id('838038', block_migration=True,
                             stress_instances={'volume_backed': False})
    @pytest.mark.testrail_id('838266', block_migration=False,
                             stress_instances={'volume_backed': False})
    @pytest.mark.testrail_id('838240', block_migration=False,
                             stress_instances={'volume_backed': True})
    @pytest.mark.parametrize('block_migration, stress_instances',
                             [
                                 (True, {'volume_backed': False}),
                                 (False, {'volume_backed': False}),
                                 (False, {'volume_backed': True}),

                             ],
                             ids=[
                                 'block LM',
                                 'true LM',
                                 'true LM volume-backed'
                             ],
                             indirect=['block_migration', 'stress_instances'])
    @pytest.mark.usefixtures('router', 'unlimited_live_migrations')
    def test_lm_under_network_work_multi_instances(
            self, stress_instances, keypair, big_hypervisors, block_migration):
        """LM of multiple instances under CPU workload

        Scenario:
            1. Allow unlimited concurrent live migrations
            2. Restart nova-api services on controllers and
                nova-compute services on computes
            3. Create maximum allowed number of instances on a single compute
                node and install iperf utility on it
            4. Group instances to pairs and run iperf server on firsh instances
                on each pair:
                iperf -u -s -p 5002
            5. Launch iperf client on seconf instances in pairs:
                iperf --port 5002 -u --client <vm1_fixed_ip> --len 64 \
                --bandwidth 5M --time 60 -i 10
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
        hypervisor1, _ = big_hypervisors
        clients = self.instances[::2]
        servers = self.instances[1::2]
        for server in servers:
            with self.os_conn.ssh_to_instance(self.env,
                                              server,
                                              vm_keypair=keypair,
                                              username='ubuntu') as remote:
                remote.check_call('iperf -u -s -p 5002 <&- >/dev/null 2>&1 &')

        if len(servers) < len(clients):
            servers.append(servers[-1])
        for client, server in zip(clients, servers):

            server_ip = self.os_conn.get_nova_instance_ips(server)['fixed']
            with self.os_conn.ssh_to_instance(self.env,
                                              client,
                                              vm_keypair=keypair,
                                              username='ubuntu') as remote:
                remote.check_call(
                    'iperf -u -c {ip} -p 5002 -t 240 --len 64 --bandwidth 5M '
                    '<&- >/dev/null 2>&1 &'.format(ip=server_ip))

        self.successive_migration(block_migration, hypervisor_from=hypervisor1)

        self.os_conn.wait_servers_ssh_ready(self.instances)

        self.os_conn.wait_hypervisor_be_free(hypervisor1)

        self.concurrent_migration(block_migration, hypervisor_to=hypervisor1)

        self.os_conn.wait_servers_ssh_ready(self.instances)


class TestLiveMigrationWithFeatures(TestLiveMigrationBase):

    @pytest.fixture(scope='class')
    def block_migration(self, request, nova_ceph):
        value = request.param
        if nova_ceph and value:
            pytest.skip('Block migration requires Nova CephRBD to be disabled')
        return value

    @pytest.yield_fixture
    def feature(self, env, request):
        feature = request.param
        nova_config = '/etc/nova/nova.conf'
        computes = env.get_nodes_by_role('compute')

        # Get old value of live_migration_flag
        with computes[0].ssh() as remote:
            parser = configparser.RawConfigParser()
            with remote.open(nova_config) as f:
                parser.readfp(f)
            old = parser.get('libvirt', 'live_migration_flag')

        config = [
            ('libvirt', 'live_migration_flag', "{0},{1}".format(old, feature))]
        if feature == 'VIR_MIGRATE_TUNNELLED':
            config.append(
                ('libvirt', 'live_migration_uri', "qemu+tcp://%s/system"))
        for step in service.nova_patch(env, config, computes):
            yield step

    @pytest.fixture
    def instances(self, request, ubuntu_image_id, os_conn, nova_ceph,
                  block_migration, big_hypervisors):
        project_id = os_conn.session.get_project_id()
        max_volumes = os_conn.cinder.quotas.get(project_id).volumes
        params = getattr(request, 'param', {'volume_backed': False})
        self.check_lm_restrictions(nova_ceph, params['volume_backed'],
                                   block_migration)

        hypervisor1, hypervisor2 = big_hypervisors
        flavor = self.os_conn.nova.flavors.find(name='m1.small')
        instances_count = min(
            self.os_conn.get_hypervisor_capacity(hypervisor1, flavor),
            self.os_conn.get_hypervisor_capacity(hypervisor2, flavor))
        instances_zone = 'nova:{0.hypervisor_hostname}'.format(hypervisor1)
        create_args = None
        if params['volume_backed']:
            instances_count = min(instances_count, max_volumes)
            create_args = []
            for i in range(instances_count):
                vol = common.create_volume(self.os_conn.cinder,
                                           ubuntu_image_id, size=5)
                create_args.append(dict(block_device_mapping={'vda': vol.id}))
                self.volumes.append(vol)
            request.addfinalizer(lambda: os_conn.delete_volumes(self.volumes))
        self.create_instances(zone=instances_zone, flavor=flavor,
                              instances_count=instances_count,
                              image_id=ubuntu_image_id,
                              create_args=create_args)
        request.addfinalizer(lambda: self.delete_instances())
        return self.instances

    @pytest.mark.testrail_id('838040', block_migration=True,
                             instances={'volume_backed': False},
                             feature='VIR_MIGRATE_AUTO_CONVERGE')
    @pytest.mark.testrail_id('838267', block_migration=False,
                             instances={'volume_backed': False},
                             feature='VIR_MIGRATE_AUTO_CONVERGE')
    @pytest.mark.testrail_id('838241', block_migration=False,
                             instances={'volume_backed': True},
                             feature='VIR_MIGRATE_AUTO_CONVERGE')
    @pytest.mark.testrail_id('838041', block_migration=True,
                             instances={'volume_backed': False},
                             feature='VIR_MIGRATE_TUNNELLED')
    @pytest.mark.testrail_id('838268', block_migration=False,
                             instances={'volume_backed': False},
                             feature='VIR_MIGRATE_TUNNELLED')
    @pytest.mark.testrail_id('838242', block_migration=False,
                             instances={'volume_backed': True},
                             feature='VIR_MIGRATE_TUNNELLED')
    @pytest.mark.parametrize(
        'block_migration, instances, feature',
        [
            (True, {'volume_backed': False}, 'VIR_MIGRATE_AUTO_CONVERGE'),
            (False, {'volume_backed': False}, 'VIR_MIGRATE_AUTO_CONVERGE'),
            (False, {'volume_backed': True}, 'VIR_MIGRATE_AUTO_CONVERGE'),
            (True, {'volume_backed': False}, 'VIR_MIGRATE_TUNNELLED'),
            (False, {'volume_backed': False}, 'VIR_MIGRATE_TUNNELLED'),
            (False, {'volume_backed': True}, 'VIR_MIGRATE_TUNNELLED')
        ],
        ids=[
            'block LM ephemeral with VIR_MIGRATE_AUTO_CONVERGE',
            'true LM ephemeral with VIR_MIGRATE_AUTO_CONVERGE',
            'true LM volume with VIR_MIGRATE_AUTO_CONVERGE',
            'block LM ephemeral with VIR_MIGRATE_TUNNELLED',
            'true LM ephemeral with VIR_MIGRATE_TUNNELLED',
            'true LM volume with VIR_MIGRATE_TUNNELLED'
        ],
        indirect=['block_migration', 'instances', 'feature'])
    @pytest.mark.usefixtures('unlimited_live_migrations', 'feature', 'router')
    def test_live_migration_with_feature(self, instances, block_migration,
                                         big_hypervisors):
        """LM of multiple instances with auto-converge or tunnelling features

        Scenario:
            1. Allow unlimited concurrent live migrations
            2. Enable required feature
            3. Restart nova-api services on controllers and
                nova-compute services on computes
            4. Create maximum allowed number of instances on a single compute
                node and install stress utilities on it
            5. Initiate serial block LM of previously created instances
                to another compute node
            6. Check that all live-migrated instances are hosted on target host
                and are in Active state:
            7. Send pings between pairs of VMs to check that network
                connectivity between these hosts is still alive
            8. Initiate concurrent block LM of previously created instances
                to another compute node
            9. Check that all live-migrated instances are hosted on target host
                and are in Active state
            10. Send pings between pairs of VMs to check that network
                connectivity between these hosts is alive
        """
        hypervisor1, _ = big_hypervisors

        self.successive_migration(block_migration, hypervisor_from=hypervisor1)

        self.os_conn.wait_servers_ssh_ready(self.instances)

        self.os_conn.wait_hypervisor_be_free(hypervisor1)

        self.concurrent_migration(block_migration, hypervisor_to=hypervisor1)

        self.os_conn.wait_servers_ssh_ready(self.instances)

    @pytest.mark.testrail_id('838042', block_migration=True,
                             stress_instances={'volume_backed': False})
    @pytest.mark.testrail_id('838269', block_migration=False,
                             stress_instances={'volume_backed': False})
    @pytest.mark.testrail_id('838243', block_migration=False,
                             stress_instances={'volume_backed': True})
    @pytest.mark.parametrize('feature', ['VIR_MIGRATE_COMPRESSED'],
                             indirect=['feature'])
    @pytest.mark.parametrize('block_migration, stress_instances',
                             [(True, {'volume_backed': False}),
                              (False, {'volume_backed': False}),
                              (False, {'volume_backed': True})],
                             ids=['block LM ephemeral',
                                  'true LM ephemeral',
                                  'true LM volume'],
                             indirect=['block_migration', 'stress_instances'])
    @pytest.mark.usefixtures('unlimited_live_migrations', 'feature', 'router')
    def test_lm_of_multiple_instances_xbzrle_compression(
            self, stress_instances, keypair, big_hypervisors, block_migration):
        """LM of multiple instances under CPU workload

        Scenario:
            1. Allow unlimited concurrent live migrations
            2. Allow VIR_MIGRATE_COMPRESSED
            3. Restart nova-api services on controllers and
                nova-compute services on computes
            4. Create maximum allowed number of instances on a single compute
                node and install iperf utility on it
            5. Group instances to pairs and run iperf server on firsh instances
                on each pair:
                iperf -u -s -p 5002
            6. Launch iperf client on seconf instances in pairs:
                iperf --port 5002 -u --client <vm1_fixed_ip> --len 64 \
                --bandwidth 5M --time 60 -i 10
            7. Initiate serial block LM of previously created instances
                to another compute node
            8. Check that all live-migrated instances are hosted on target host
                and are in Active state:
            9. Send pings between pairs of VMs to check that network
                connectivity between these hosts is still alive
            10. Initiate concurrent block LM of previously created instances
                to another compute node
            11. Check that all live-migrated instances are hosted on target
                host and are in Active state
            12. Send pings between pairs of VMs to check that network
                connectivity between these hosts is alive
        """
        hypervisor1, _ = big_hypervisors
        clients = self.instances[::2]
        servers = self.instances[1::2]
        for server in servers:
            with self.os_conn.ssh_to_instance(self.env,
                                              server,
                                              vm_keypair=keypair,
                                              username='ubuntu') as remote:
                remote.background_call('iperf -u -s -p 5002')

        if len(servers) < len(clients):
            servers.append(servers[-1])
        for client, server in zip(clients, servers):

            server_ip = self.os_conn.get_nova_instance_ips(server)['fixed']
            with self.os_conn.ssh_to_instance(self.env,
                                              client,
                                              vm_keypair=keypair,
                                              username='ubuntu') as remote:
                remote.background_call(
                    'iperf -u -c {ip} -p 5002 -t 240 --len 64 --bandwidth 5M'
                    .format(ip=server_ip))

        self.successive_migration(block_migration, hypervisor_from=hypervisor1)

        self.os_conn.wait_servers_ssh_ready(self.instances)

        self.os_conn.wait_hypervisor_be_free(hypervisor1)

        self.concurrent_migration(block_migration, hypervisor_to=hypervisor1)

        self.os_conn.wait_servers_ssh_ready(self.instances)


class TestLiveMigrationWithUserContent(TestLiveMigrationBase):

    userdata = '\n'.join(["#!/bin/bash -v", "echo 'Hello world!'"])

    @pytest.fixture(scope='class')
    def block_migration(self, request, nova_ceph):
        value = request.param
        if nova_ceph and value:
            pytest.skip('Block migration requires Nova CephRBD to be disabled')
        return value

    @pytest.fixture
    def instance(self, request, ubuntu_image_id, nova_ceph, block_migration):
        params = getattr(request, 'param', {'volume_backed': False})

        # To be removed in MOS 10.0
        if not nova_ceph and params['volume_backed']:
            pytest.skip("Volume-backed instances with config-drive=true fail "
                        "to live-migrate without nova ceph. Launchpad bug: "
                        "https://bugs.launchpad.net/mos/+bug/1589460/")
        self.check_lm_restrictions(nova_ceph, params['volume_backed'],
                                   block_migration)
        flavor = self.os_conn.nova.flavors.find(name='m1.small')

        # config_drive=True to be removed in MOS 10.0 after WA removal
        # https://bugs.launchpad.net/mos/+bug/1589460/
        create_args = [dict(meta={'role': 'webservers', 'essential': 'false'},
                            config_drive=True)]

        if params['volume_backed']:
            vol = common.create_volume(self.os_conn.cinder, ubuntu_image_id,
                                       size=5)
            create_args[0].update(dict(block_device_mapping={'vda': vol.id}))
            self.volumes.append(vol)
            request.addfinalizer(
                lambda: self.os_conn.delete_volumes(self.volumes))
        self.create_instances('nova', flavor, 1, image_id=ubuntu_image_id,
                              userdata=self.userdata, create_args=create_args)
        request.addfinalizer(lambda: self.delete_instances())
        return self.instances[0]

    @pytest.fixture
    def drive_format(self, request):
        return request.param

    @pytest.yield_fixture
    def config_drive_format(self, drive_format):
        computes = self.env.get_nodes_by_role('compute')
        config = [('DEFAULT', 'config_drive_format', drive_format)]
        for step in service.nova_patch(self.env, config, computes):
            yield step

    @pytest.yield_fixture
    def genisoimage(self):
        computes = self.env.get_nodes_by_role('compute')
        for node in computes:
            with node.ssh() as remote:
                remote.check_call("apt-get install -y genisoimage")

        yield
        for node in computes:
            with node.ssh() as remote:
                remote.check_call("apt-get remove -y genisoimage")

    def get_instance_data(self, vm, vm_keypair, config_format,
                          username='ubuntu', mount_required=True):

        with self.os_conn.ssh_to_instance(self.env, vm, vm_keypair=vm_keypair,
                                          username=username) as remote:
            devices = remote.check_call("cat /proc/partitions")['stdout']
            device = "/dev/{0}".format(devices[-1].split()[-1])
            mnt_dir = "/mnt/config"

            if mount_required:
                logger.info("Mount {0} to {1}".format(device, mnt_dir))
                remote.check_call("sudo mkdir -p {0}".format(mnt_dir))
                remote.check_call("sudo mount {0} {1}".format(device, mnt_dir))

            res = remote.check_call("sudo ls {0}/openstack/latest/"
                                    .format(mnt_dir))
            files = set([r.strip() for r in res['stdout']])
            try:
                data_path = '{0}/openstack/latest/user_data'.format(mnt_dir)
                with remote.open(data_path) as f:
                    userdata_content = f.read()
            except IOError:
                raise AssertionError("Unable to find userdata")
            return files, userdata_content

    @pytest.mark.testrail_id('842896', block_migration=True,
                             instance={'volume_backed': False},
                             drive_format='vfat')
    @pytest.mark.testrail_id('842491', block_migration=False,
                             instance={'volume_backed': False},
                             drive_format='vfat')
    @pytest.mark.testrail_id('842492', block_migration=False,
                             instance={'volume_backed': True},
                             drive_format='vfat')
    @pytest.mark.testrail_id('842519', block_migration=False,
                             instance={'volume_backed': False},
                             drive_format='iso9660')
    @pytest.mark.testrail_id('842520', block_migration=False,
                             instance={'volume_backed': True},
                             drive_format='iso9660')
    @pytest.mark.parametrize(
        'block_migration, instance, drive_format',
        [
            (True, {'volume_backed': False}, 'vfat'),
            (False, {'volume_backed': False}, 'vfat'),
            (False, {'volume_backed': True}, 'vfat'),
            (False, {'volume_backed': False}, 'iso9660'),
            (False, {'volume_backed': True}, 'iso9660'),
        ],
        ids=[
            'block LM ephemeral with vfat',
            'true LM ephemeral with vfat',
            'true LM volume with vfat',
            'true LM ephemeral with iso9660',
            'true LM volume with iso9660',
        ],
        indirect=['block_migration', 'instance', 'drive_format'])
    @pytest.mark.usefixtures('unlimited_live_migrations', 'genisoimage',
                             'config_drive_format', 'router')
    def test_lm_with_user_content_and_config_drive(self, drive_format,
                                                   instance, keypair,
                                                   block_migration):

        """LM of instance with user content and configuration drive

        Scenario:
            1. Set required configuration drive on computes
            2. Install 'genisoimage' in case of config_drive_format=iso9660
            3. Restart nova-api services on controllers and
                nova-compute services on computes if any change in nova.conf
            4. Boot an instance with config drive and user script
            5. Login to instance and mount specific device.
            6. Check that config drive contains all data carried to it during
                instance creation
            7. Initiate LM of the instance to another compute node
            8. Check that live-migrated instance is hosted on target host
            9. Login to instance again and re-check config drive
        """
        old_files, old_userdata = self.get_instance_data(instance, keypair,
                                                         drive_format)
        assert self.userdata in old_userdata, (
            "Unexpected content of userdata before migrate: "
            "should be the same as for instance boot")

        old_host = getattr(instance, 'OS-EXT-SRV-ATTR:host')
        instance.live_migrate(block_migration=block_migration)

        common.wait(
            lambda: is_migrated(self.os_conn, [instance], source=old_host),
            timeout_seconds=5 * 60,
            waiting_for='instance to migrate from {0}'.format(old_host))
        common.wait(lambda: self.os_conn.is_server_ssh_ready(instance),
                    timeout_seconds=2 * 60,
                    waiting_for='instance to be available via ssh')

        new_files, new_userdata = self.get_instance_data(instance, keypair,
                                                         drive_format,
                                                         mount_required=False)
        assert self.userdata in new_userdata, (
            "Unexpected content of userdata after migrate: "
            "should be the same as for instance boot")
        assert old_files == new_files, (
            "List of files after live migration is not equal to initial one. "
            "Files to check: {0}\n".format(list(new_files ^ old_files)))
