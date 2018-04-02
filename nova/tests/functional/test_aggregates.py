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

import time

from nova.scheduler.client import report

from nova import context as nova_context
from nova import test
from nova.tests import fixtures as nova_fixtures
from nova.tests.functional import integrated_helpers
import nova.tests.unit.image.fake
from nova.tests.unit import policy_fixture
from nova.virt import fake


class AggregatesTest(integrated_helpers._IntegratedTestBase):
    api_major_version = 'v2'
    ADMIN_API = True

    def _add_hosts_to_aggregate(self):
        """List all compute services and add them all to an aggregate."""

        compute_services = [s for s in self.api.get_services()
                            if s['binary'] == 'nova-compute']
        agg = {'aggregate': {'name': 'test-aggregate'}}
        agg = self.api.post_aggregate(agg)
        for service in compute_services:
            self.api.add_host_to_aggregate(agg['id'], service['host'])
        return len(compute_services)

    def test_add_hosts(self):
        # Default case with one compute, mapped for us
        self.assertEqual(1, self._add_hosts_to_aggregate())

    def test_add_unmapped_host(self):
        """Ensure that hosts without mappings are still found and added"""

        # Add another compute, but nuke its HostMapping
        self.start_service('compute', host='compute2')
        self.host_mappings['compute2'].destroy()
        self.assertEqual(2, self._add_hosts_to_aggregate())


class AggregateRequestFiltersTest(test.TestCase,
                                  integrated_helpers.InstanceHelperMixin):
    microversion = 'latest'
    compute_driver = 'fake.MediumFakeDriver'

    def setUp(self):
        self.flags(compute_driver=self.compute_driver)
        super(AggregateRequestFiltersTest, self).setUp()

        self.useFixture(policy_fixture.RealPolicyFixture())
        self.useFixture(nova_fixtures.NeutronFixture(self))
        self.useFixture(nova_fixtures.AllServicesCurrent())

        placement = self.useFixture(nova_fixtures.PlacementFixture())
        self.placement_api = placement.api
        api_fixture = self.useFixture(nova_fixtures.OSAPIFixture(
            api_version='v2.1'))

        self.admin_api = api_fixture.admin_api
        self.admin_api.microversion = self.microversion
        self.api = self.admin_api

        # the image fake backend needed for image discovery
        nova.tests.unit.image.fake.stub_out_image_service(self)

        self.start_service('conductor')
        self.scheduler_service = self.start_service('scheduler')

        self.computes = {}
        self.aggregates = {}

        self._start_compute('host1')
        self._start_compute('host2')

        self.context = nova_context.get_admin_context()
        self.report_client = report.SchedulerReportClient()

        self.flavors = self.api.get_flavors()

        # Aggregate with only host1
        self._create_aggregate('only-host1')
        self._add_host_to_aggregate('only-host1', 'host1')

        # Aggregate with only host2
        self._create_aggregate('only-host2')
        self._add_host_to_aggregate('only-host2', 'host2')

        # Aggregate with neither host
        self._create_aggregate('no-hosts')

        # Default to enabling the filter and making it mandatory
        self.flags(limit_tenants_to_placement_aggregate=True,
                   group='scheduler')
        self.flags(placement_aggregate_required_for_tenants=True,
                   group='scheduler')

    def _start_compute(self, host):
        """Start a nova compute service on the given host

        :param host: the name of the host that will be associated to the
                     compute service.
        :return: the nova compute service object
        """
        fake.set_nodes([host])
        self.addCleanup(fake.restore_nodes)
        compute = self.start_service('compute', host=host)
        self.computes[host] = compute
        return compute

    def _create_aggregate(self, name):
        agg = self.admin_api.post_aggregate({'aggregate': {'name': name}})
        self.aggregates[name] = agg

    def _get_provider_uuid_by_host(self, host):
        """Return the compute node uuid for a named compute host."""
        # NOTE(gibi): the compute node id is the same as the compute node
        # provider uuid on that compute
        resp = self.admin_api.api_get(
            'os-hypervisors?hypervisor_hostname_pattern=%s' % host).body
        return resp['hypervisors'][0]['id']

    def _add_host_to_aggregate(self, agg, host):
        """Add a compute host to both nova and placement aggregates.

        :param agg: Name of the nova aggregate
        :param host: Name of the compute host
        """
        agg = self.aggregates[agg]
        self.admin_api.add_host_to_aggregate(agg['id'], host)

        host_uuid = self._get_provider_uuid_by_host(host)

        # Get the existing aggregates for this host in placement and add the
        # new one to it
        aggs = self.report_client.get(
            '/resource_providers/%s/aggregates' % host_uuid,
            version='1.1').json()
        placement_aggs = aggs['aggregates']
        placement_aggs.append(agg['uuid'])

        # Make sure we have a view of the provider we're about to mess with
        # FIXME(efried): This should be a thing we can do without internals
        self.report_client._ensure_resource_provider(self.context, host_uuid)

        self.report_client.set_aggregates_for_provider(self.context, host_uuid,
                                                       placement_aggs)

    def _grant_tenant_aggregate(self, agg, tenants):
        """Grant a set of tenants access to use an aggregate.

        :param agg: Name of the nova aggregate
        :param tenants: A list of all tenant ids that will be allowed access
        """
        agg = self.aggregates[agg]
        action = {
            'set_metadata': {
                'metadata': {
                    'filter_tenant_id%i' % i: tenant
                    for i, tenant in enumerate(tenants)
                }
            },
        }
        self.admin_api.post_aggregate_action(agg['id'], action)

    def _wait_for_state_change(self, server, from_status):
        for i in range(0, 50):
            server = self.api.get_server(server['id'])
            if server['status'] != from_status:
                break
            time.sleep(.1)

        return server

    def _boot_server(self):
        server_req = self._build_minimal_create_server_request(
            self.api, 'test-instance', flavor_id=self.flavors[0]['id'],
            image_uuid='155d900f-4e14-4e4c-a73d-069cbf4541e6',
            networks='none')

        created_server = self.api.post_server({'server': server_req})
        server = self._wait_for_state_change(created_server, 'BUILD')

        return server

    def test_tenant_id_required_fails_if_no_aggregate(self):
        server = self._boot_server()
        # Without granting our tenant permission to an aggregate, instance
        # creates should fail since aggregates are required
        self.assertEqual('ERROR', server['status'])

    def test_tenant_id_not_required_succeeds_if_no_aggregate(self):
        self.flags(placement_aggregate_required_for_tenants=False,
                   group='scheduler')
        server = self._boot_server()
        # Without granting our tenant permission to an aggregate, instance
        # creates should still succeed since aggregates are not required
        self.assertEqual('ACTIVE', server['status'])

    def _get_instance_host(self, server):
        srv = self.admin_api.get_server(server['id'])
        return srv['OS-EXT-SRV-ATTR:host']

    def test_filter_honors_tenant_id(self):
        tenant = self.api.project_id

        # Grant our tenant access to the aggregate with only host1 in it
        # and boot some servers. They should all stack up on host1.
        self._grant_tenant_aggregate('only-host1',
                                     ['foo', tenant, 'bar'])
        server1 = self._boot_server()
        server2 = self._boot_server()
        self.assertEqual('ACTIVE', server1['status'])
        self.assertEqual('ACTIVE', server2['status'])

        # Grant our tenant access to the aggregate with only host2 in it
        # and boot some servers. They should all stack up on host2.
        self._grant_tenant_aggregate('only-host1',
                                     ['foo', 'bar'])
        self._grant_tenant_aggregate('only-host2',
                                     ['foo', tenant, 'bar'])
        server3 = self._boot_server()
        server4 = self._boot_server()
        self.assertEqual('ACTIVE', server3['status'])
        self.assertEqual('ACTIVE', server4['status'])

        # Make sure the servers landed on the hosts we had access to at
        # the time we booted them.
        hosts = [self._get_instance_host(s)
                 for s in (server1, server2, server3, server4)]
        expected_hosts = ['host1', 'host1', 'host2', 'host2']
        self.assertEqual(expected_hosts, hosts)

    def test_filter_with_empty_aggregate(self):
        tenant = self.api.project_id

        # Grant our tenant access to the aggregate with no hosts in it
        self._grant_tenant_aggregate('no-hosts',
                                     ['foo', tenant, 'bar'])
        server = self._boot_server()
        self.assertEqual('ERROR', server['status'])

    def test_filter_with_multiple_aggregates_for_tenant(self):
        tenant = self.api.project_id

        # Grant our tenant access to the aggregate with no hosts in it,
        # and one with a host.
        self._grant_tenant_aggregate('no-hosts',
                                     ['foo', tenant, 'bar'])
        self._grant_tenant_aggregate('only-host2',
                                     ['foo', tenant, 'bar'])

        # Boot several servers and make sure they all land on the
        # only host we have access to.
        for i in range(0, 4):
            server = self._boot_server()
            self.assertEqual('ACTIVE', server['status'])
            self.assertEqual('host2', self._get_instance_host(server))
