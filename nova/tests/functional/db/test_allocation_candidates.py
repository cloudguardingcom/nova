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
import os_traits
from oslo_utils import uuidutils

from nova.api.openstack.placement import lib as placement_lib
from nova import context
from nova import exception
from nova.objects import fields
from nova.objects import resource_provider as rp_obj
from nova import test
from nova.tests import fixtures
from nova.tests import uuidsentinel as uuids


def _add_inventory(rp, rc, total, **kwargs):
    kwargs.setdefault('max_unit', total)
    inv = rp_obj.Inventory(rp._context, resource_provider=rp,
                           resource_class=rc, total=total, **kwargs)
    inv.obj_set_defaults()
    rp.add_inventory(inv)


def _set_traits(rp, *traits):
    tlist = []
    for tname in traits:
        try:
            trait = rp_obj.Trait.get_by_name(rp._context, tname)
        except exception.TraitNotFound:
            trait = rp_obj.Trait(rp._context, name=tname)
            trait.create()
        tlist.append(trait)
    rp.set_traits(rp_obj.TraitList(objects=tlist))


def _allocate_from_provider(rp, rc, used):
    # NOTE(efried): Always use a random consumer UUID - we don't want to
    # override any existing allocations from the test case.
    rp_obj.AllocationList(
        rp._context, objects=[
            rp_obj.Allocation(
                rp._context, resource_provider=rp, resource_class=rc,
                consumer_id=uuidutils.generate_uuid(), used=used)]
    ).create_all()


def _provider_uuids_from_iterable(objs):
    """Return the set of resource_provider.uuid from an iterable.

    :param objs: Iterable of any object with a resource_provider attribute
                 (e.g. an AllocationRequest.resource_requests or an
                 AllocationCandidates.provider_summaries).
    """
    return set(obj.resource_provider.uuid for obj in objs)


def _find_summary_for_provider(p_sums, rp_uuid):
    for summary in p_sums:
        if summary.resource_provider.uuid == rp_uuid:
            return summary


def _find_summary_for_resource(p_sum, rc_name):
    for resource in p_sum.resources:
        if resource.resource_class == rc_name:
            return resource


class ProviderDBBase(test.NoDBTestCase):

    USES_DB_SELF = True

    def setUp(self):
        super(ProviderDBBase, self).setUp()
        self.useFixture(fixtures.Database())
        self.api_db = self.useFixture(fixtures.Database(database='api'))
        self.ctx = context.RequestContext('fake-user', 'fake-project')
        self.requested_resources = {
            fields.ResourceClass.VCPU: 1,
            fields.ResourceClass.MEMORY_MB: 64,
            fields.ResourceClass.DISK_GB: 1500,
        }
        # For debugging purposes, populated by _create_provider and used by
        # _validate_allocation_requests to make failure results more readable.
        self.rp_uuid_to_name = {}

    def _create_provider(self, name, *aggs):
        rp = rp_obj.ResourceProvider(self.ctx, name=name,
                                     uuid=getattr(uuids, name))
        rp.create()
        if aggs:
            rp.set_aggregates(aggs)
        self.rp_uuid_to_name[rp.uuid] = name
        return rp


class ProviderDBHelperTestCase(ProviderDBBase):

    def test_get_provider_ids_matching_all(self):
        # These RPs are named based on whether we expect them to be 'incl'uded
        # or 'excl'uded in the result.

        # No inventory records.  This one should never show up in a result.
        self._create_provider('no_inventory')

        # Inventory of adequate CPU and memory, no allocations against it.
        excl_big_cm_noalloc = self._create_provider('big_cm_noalloc')
        _add_inventory(excl_big_cm_noalloc, fields.ResourceClass.VCPU, 15)
        _add_inventory(excl_big_cm_noalloc, fields.ResourceClass.MEMORY_MB,
                       4096, max_unit=2048)

        # Adequate inventory, no allocations against it.
        incl_biginv_noalloc = self._create_provider('biginv_noalloc')
        _add_inventory(incl_biginv_noalloc, fields.ResourceClass.VCPU, 15)
        _add_inventory(incl_biginv_noalloc, fields.ResourceClass.MEMORY_MB,
                       4096, max_unit=2048)
        _add_inventory(incl_biginv_noalloc, fields.ResourceClass.DISK_GB, 2000)

        # No allocations, but inventory unusable.  Try to hit all the possible
        # reasons for exclusion.
        # VCPU min_unit too high
        excl_badinv_min_unit = self._create_provider('badinv_min_unit')
        _add_inventory(excl_badinv_min_unit, fields.ResourceClass.VCPU, 12,
                       min_unit=6)
        _add_inventory(excl_badinv_min_unit, fields.ResourceClass.MEMORY_MB,
                       4096, max_unit=2048)
        _add_inventory(excl_badinv_min_unit, fields.ResourceClass.DISK_GB,
                       2000)
        # MEMORY_MB max_unit too low
        excl_badinv_max_unit = self._create_provider('badinv_max_unit')
        _add_inventory(excl_badinv_max_unit, fields.ResourceClass.VCPU, 15)
        _add_inventory(excl_badinv_max_unit, fields.ResourceClass.MEMORY_MB,
                       4096, max_unit=512)
        _add_inventory(excl_badinv_max_unit, fields.ResourceClass.DISK_GB,
                       2000)
        # DISK_GB unsuitable step_size
        excl_badinv_step_size = self._create_provider('badinv_step_size')
        _add_inventory(excl_badinv_step_size, fields.ResourceClass.VCPU, 15)
        _add_inventory(excl_badinv_step_size, fields.ResourceClass.MEMORY_MB,
                       4096, max_unit=2048)
        _add_inventory(excl_badinv_step_size, fields.ResourceClass.DISK_GB,
                       2000, step_size=7)
        # Not enough total VCPU
        excl_badinv_total = self._create_provider('badinv_total')
        _add_inventory(excl_badinv_total, fields.ResourceClass.VCPU, 4)
        _add_inventory(excl_badinv_total, fields.ResourceClass.MEMORY_MB,
                       4096, max_unit=2048)
        _add_inventory(excl_badinv_total, fields.ResourceClass.DISK_GB, 2000)
        # Too much reserved MEMORY_MB
        excl_badinv_reserved = self._create_provider('badinv_reserved')
        _add_inventory(excl_badinv_reserved, fields.ResourceClass.VCPU, 15)
        _add_inventory(excl_badinv_reserved, fields.ResourceClass.MEMORY_MB,
                       4096, max_unit=2048, reserved=3500)
        _add_inventory(excl_badinv_reserved, fields.ResourceClass.DISK_GB,
                       2000)
        # DISK_GB allocation ratio blows it up
        excl_badinv_alloc_ratio = self._create_provider('badinv_alloc_ratio')
        _add_inventory(excl_badinv_alloc_ratio, fields.ResourceClass.VCPU, 15)
        _add_inventory(excl_badinv_alloc_ratio, fields.ResourceClass.MEMORY_MB,
                       4096, max_unit=2048)
        _add_inventory(excl_badinv_alloc_ratio, fields.ResourceClass.DISK_GB,
                       2000, allocation_ratio=0.5)

        # Inventory consumed in one RC, but available in the others
        excl_1invunavail = self._create_provider('1invunavail')
        _add_inventory(excl_1invunavail, fields.ResourceClass.VCPU, 10)
        _allocate_from_provider(excl_1invunavail, fields.ResourceClass.VCPU, 7)
        _add_inventory(excl_1invunavail, fields.ResourceClass.MEMORY_MB, 4096)
        _allocate_from_provider(excl_1invunavail,
                                fields.ResourceClass.MEMORY_MB, 1024)
        _add_inventory(excl_1invunavail, fields.ResourceClass.DISK_GB, 2000)
        _allocate_from_provider(excl_1invunavail,
                                fields.ResourceClass.DISK_GB, 400)

        # Inventory all consumed
        excl_allused = self._create_provider('allused')
        _add_inventory(excl_allused, fields.ResourceClass.VCPU, 10)
        _allocate_from_provider(excl_allused, fields.ResourceClass.VCPU, 7)
        _add_inventory(excl_allused, fields.ResourceClass.MEMORY_MB, 4000)
        _allocate_from_provider(excl_allused,
                                fields.ResourceClass.MEMORY_MB, 1500)
        _allocate_from_provider(excl_allused,
                                fields.ResourceClass.MEMORY_MB, 2000)
        _add_inventory(excl_allused, fields.ResourceClass.DISK_GB, 1500)
        _allocate_from_provider(excl_allused, fields.ResourceClass.DISK_GB, 1)

        # Inventory available in requested classes, but unavailable in others
        incl_extra_full = self._create_provider('extra_full')
        _add_inventory(incl_extra_full, fields.ResourceClass.VCPU, 20)
        _allocate_from_provider(incl_extra_full, fields.ResourceClass.VCPU, 15)
        _add_inventory(incl_extra_full, fields.ResourceClass.MEMORY_MB, 4096)
        _allocate_from_provider(incl_extra_full,
                                fields.ResourceClass.MEMORY_MB, 1024)
        _add_inventory(incl_extra_full, fields.ResourceClass.DISK_GB, 2000)
        _allocate_from_provider(incl_extra_full, fields.ResourceClass.DISK_GB,
                                400)
        _add_inventory(incl_extra_full, fields.ResourceClass.PCI_DEVICE, 4)
        _allocate_from_provider(incl_extra_full,
                                fields.ResourceClass.PCI_DEVICE, 1)
        _allocate_from_provider(incl_extra_full,
                                fields.ResourceClass.PCI_DEVICE, 3)

        # Inventory available in a unrequested classes, not in requested ones
        excl_extra_avail = self._create_provider('extra_avail')
        # Incompatible step size
        _add_inventory(excl_extra_avail, fields.ResourceClass.VCPU, 10,
                       step_size=3)
        # Not enough left after reserved + used
        _add_inventory(excl_extra_avail, fields.ResourceClass.MEMORY_MB, 4096,
                       max_unit=2048, reserved=2048)
        _allocate_from_provider(excl_extra_avail,
                                fields.ResourceClass.MEMORY_MB, 1040)
        # Allocation ratio math
        _add_inventory(excl_extra_avail, fields.ResourceClass.DISK_GB, 2000,
                       allocation_ratio=0.5)
        _add_inventory(excl_extra_avail, fields.ResourceClass.IPV4_ADDRESS, 48)
        custom_special = rp_obj.ResourceClass(self.ctx, name='CUSTOM_SPECIAL')
        custom_special.create()
        _add_inventory(excl_extra_avail, 'CUSTOM_SPECIAL', 100)
        _allocate_from_provider(excl_extra_avail, 'CUSTOM_SPECIAL', 99)

        resources = {
            fields.ResourceClass.STANDARD.index(fields.ResourceClass.VCPU): 5,
            fields.ResourceClass.STANDARD.index(
                fields.ResourceClass.MEMORY_MB): 1024,
            fields.ResourceClass.STANDARD.index(
                fields.ResourceClass.DISK_GB): 1500
        }

        # Run it!
        res = rp_obj._get_provider_ids_matching_all(self.ctx, resources, {})

        # We should get all the incl_* RPs
        expected = [incl_biginv_noalloc, incl_extra_full]

        self.assertEqual(set(rp.id for rp in expected), set(res))

        # Now request that the providers must have a set of required traits and
        # that this results in no results returned, since we haven't yet
        # associated any traits with the providers
        avx2_t = rp_obj.Trait.get_by_name(self.ctx, os_traits.HW_CPU_X86_AVX2)
        # _get_provider_ids_matching_all()'s required_traits argument is a map,
        # keyed by trait name, of the trait internal ID
        req_traits = {os_traits.HW_CPU_X86_AVX2: avx2_t.id}
        res = rp_obj._get_provider_ids_matching_all(self.ctx, resources,
                                                    req_traits)

        self.assertEqual([], res)

        # OK, now add the trait to one of the providers and verify that
        # provider now shows up in our results
        incl_biginv_noalloc.set_traits([avx2_t])
        res = rp_obj._get_provider_ids_matching_all(self.ctx, resources,
                                                    req_traits)

        self.assertEqual([incl_biginv_noalloc.id], res)

    def test_get_provider_ids_having_all_traits(self):
        def run(traitnames, expected_ids):
            tmap = {}
            if traitnames:
                tmap = rp_obj._trait_ids_from_names(self.ctx, traitnames)
            obs = rp_obj._get_provider_ids_having_all_traits(self.ctx, tmap)
            self.assertEqual(sorted(expected_ids), sorted(obs))

        # No traits.  This will never be returned, because it's illegal to
        # invoke the method with no traits.
        self._create_provider('one')

        # One trait
        rp2 = self._create_provider('two')
        _set_traits(rp2, 'HW_CPU_X86_TBM')

        # One the same as rp2
        rp3 = self._create_provider('three')
        _set_traits(rp3, 'HW_CPU_X86_TBM', 'HW_CPU_X86_TSX', 'HW_CPU_X86_SGX')

        # Disjoint
        rp4 = self._create_provider('four')
        _set_traits(rp4, 'HW_CPU_X86_SSE2', 'HW_CPU_X86_SSE3', 'CUSTOM_FOO')

        # Request with no traits not allowed
        self.assertRaises(
            ValueError,
            rp_obj._get_provider_ids_having_all_traits, self.ctx, None)
        self.assertRaises(
            ValueError,
            rp_obj._get_provider_ids_having_all_traits, self.ctx, {})

        # Common trait returns both RPs having it
        run(['HW_CPU_X86_TBM'], [rp2.id, rp3.id])
        # Just the one
        run(['HW_CPU_X86_TSX'], [rp3.id])
        run(['HW_CPU_X86_TSX', 'HW_CPU_X86_SGX'], [rp3.id])
        run(['CUSTOM_FOO'], [rp4.id])
        # Including the common one still just gets me rp3
        run(['HW_CPU_X86_TBM', 'HW_CPU_X86_SGX'], [rp3.id])
        run(['HW_CPU_X86_TBM', 'HW_CPU_X86_TSX', 'HW_CPU_X86_SGX'], [rp3.id])
        # Can't be satisfied
        run(['HW_CPU_X86_TBM', 'HW_CPU_X86_TSX', 'CUSTOM_FOO'], [])
        run(['HW_CPU_X86_TBM', 'HW_CPU_X86_TSX', 'HW_CPU_X86_SGX',
             'CUSTOM_FOO'], [])
        run(['HW_CPU_X86_SGX', 'HW_CPU_X86_SSE3'], [])
        run(['HW_CPU_X86_TBM', 'CUSTOM_FOO'], [])
        run(['HW_CPU_X86_BMI'], [])
        rp_obj.Trait(self.ctx, name='CUSTOM_BAR').create()
        run(['CUSTOM_BAR'], [])


class AllocationCandidatesTestCase(ProviderDBBase):
    """Tests a variety of scenarios with both shared and non-shared resource
    providers that the AllocationCandidates.get_by_requests() method returns a
    set of alternative allocation requests and provider summaries that may be
    used by the scheduler to sort/weigh the options it has for claiming
    resources against providers.
    """

    def setUp(self):
        super(AllocationCandidatesTestCase, self).setUp()
        self.requested_resources = {
            fields.ResourceClass.VCPU: 1,
            fields.ResourceClass.MEMORY_MB: 64,
            fields.ResourceClass.DISK_GB: 1500,
        }
        # For debugging purposes, populated by _create_provider and used by
        # _validate_allocation_requests to make failure results more readable.
        self.rp_uuid_to_name = {}

    def _get_allocation_candidates(self, requests=None):
        if requests is None:
            requests = [placement_lib.RequestGroup(
                use_same_provider=False,
                resources=self.requested_resources)]
        return rp_obj.AllocationCandidates.get_by_requests(self.ctx, requests)

    def _validate_allocation_requests(self, expected, candidates):
        """Assert correctness of allocation requests in allocation candidates.

        This is set up to make it easy for the caller to specify the expected
        result, to make that expected structure readable for someone looking at
        the test case, and to make test failures readable for debugging.

        :param expected: A list of lists of tuples representing the expected
                         allocation requests, of the form:
             [
                [(resource_provider_name, resource_class_name, resource_count),
                 ...,
                ],
                ...
             ]
        :param candidates: The result from AllocationCandidates.get_by_requests
        """
        # Extract/convert allocation requests from candidates
        observed = []
        for ar in candidates.allocation_requests:
            rrs = []
            for rr in ar.resource_requests:
                rrs.append((self.rp_uuid_to_name[rr.resource_provider.uuid],
                            rr.resource_class, rr.amount))
            rrs.sort()
            observed.append(rrs)
        observed.sort()

        # Sort the guts of the expected structure
        for rr in expected:
            rr.sort()
        expected.sort()

        # Now we ought to be able to compare them
        self.assertEqual(expected, observed)

    def test_no_resources_in_first_request_group(self):
        requests = [placement_lib.RequestGroup(use_same_provider=False,
                                               resources={})]
        self.assertRaises(ValueError,
                          rp_obj.AllocationCandidates.get_by_requests,
                          self.ctx, requests)

    def test_unknown_traits(self):
        missing = set(['UNKNOWN_TRAIT'])
        requests = [placement_lib.RequestGroup(
            use_same_provider=False, resources=self.requested_resources,
            required_traits=missing)]
        self.assertRaises(ValueError,
                          rp_obj.AllocationCandidates.get_by_requests,
                          self.ctx, requests)

    def test_all_local(self):
        """Create some resource providers that can satisfy the request for
        resources with local (non-shared) resources and verify that the
        allocation requests returned by AllocationCandidates correspond with
        each of these resource providers.
        """
        # Create three compute node providers with VCPU, RAM and local disk
        cn1, cn2, cn3 = (self._create_provider(name)
                         for name in ('cn1', 'cn2', 'cn3'))
        for cn in (cn1, cn2, cn3):
            _add_inventory(cn, fields.ResourceClass.VCPU, 24,
                           allocation_ratio=16.0)
            _add_inventory(cn, fields.ResourceClass.MEMORY_MB, 32768,
                           min_unit=64, step_size=64, allocation_ratio=1.5)
            total_gb = 1000 if cn.name == 'cn3' else 2000
            _add_inventory(cn, fields.ResourceClass.DISK_GB, total_gb,
                           reserved=100, min_unit=10, step_size=10,
                           allocation_ratio=1.0)

        # Ask for the alternative placement possibilities and verify each
        # provider is returned
        alloc_cands = self._get_allocation_candidates()

        # Verify the provider summary information indicates 0 usage and
        # capacity calculated from above inventory numbers for both compute
        # nodes
        # TODO(efried): Come up with a terse & readable way to validate
        # provider summaries
        p_sums = alloc_cands.provider_summaries

        self.assertEqual(set([uuids.cn1, uuids.cn2]),
                         _provider_uuids_from_iterable(p_sums))

        cn1_p_sum = _find_summary_for_provider(p_sums, uuids.cn1)
        self.assertIsNotNone(cn1_p_sum)
        self.assertEqual(3, len(cn1_p_sum.resources))

        cn1_p_sum_vcpu = _find_summary_for_resource(cn1_p_sum, 'VCPU')
        self.assertIsNotNone(cn1_p_sum_vcpu)

        expected_capacity = (24 * 16.0)
        self.assertEqual(expected_capacity, cn1_p_sum_vcpu.capacity)
        self.assertEqual(0, cn1_p_sum_vcpu.used)

        # Let's verify the disk for the second compute node
        cn2_p_sum = _find_summary_for_provider(p_sums, uuids.cn2)
        self.assertIsNotNone(cn2_p_sum)
        self.assertEqual(3, len(cn2_p_sum.resources))

        cn2_p_sum_disk = _find_summary_for_resource(cn2_p_sum, 'DISK_GB')
        self.assertIsNotNone(cn2_p_sum_disk)

        expected_capacity = ((2000 - 100) * 1.0)
        self.assertEqual(expected_capacity, cn2_p_sum_disk.capacity)
        self.assertEqual(0, cn2_p_sum_disk.used)

        # Verify the allocation requests that are returned. There should be 2
        # allocation requests, one for each compute node, containing 3
        # resources in each allocation request, one each for VCPU, RAM, and
        # disk. The amounts of the requests should correspond to the requested
        # resource amounts in the filter:resources dict passed to
        # AllocationCandidates.get_by_requests().
        expected = [
            [('cn1', fields.ResourceClass.VCPU, 1),
             ('cn1', fields.ResourceClass.MEMORY_MB, 64),
             ('cn1', fields.ResourceClass.DISK_GB, 1500)],
            [('cn2', fields.ResourceClass.VCPU, 1),
             ('cn2', fields.ResourceClass.MEMORY_MB, 64),
             ('cn2', fields.ResourceClass.DISK_GB, 1500)],
        ]
        self._validate_allocation_requests(expected, alloc_cands)

        # Now let's add traits into the mix. Currently, none of the compute
        # nodes has the AVX2 trait associated with it, so we should get 0
        # results if we required AVX2
        alloc_cands = rp_obj.AllocationCandidates.get_by_requests(
            self.ctx,
            requests=[placement_lib.RequestGroup(
                use_same_provider=False,
                resources=self.requested_resources,
                required_traits=set([os_traits.HW_CPU_X86_AVX2])
            )],
        )
        self._validate_allocation_requests([], alloc_cands)

        # If we then associate the AVX2 trait to just compute node 2, we should
        # get back just that compute node in the provider summaries
        _set_traits(cn2, 'HW_CPU_X86_AVX2')

        alloc_cands = rp_obj.AllocationCandidates.get_by_requests(
            self.ctx,
            requests=[placement_lib.RequestGroup(
                use_same_provider=False,
                resources=self.requested_resources,
                required_traits=set([os_traits.HW_CPU_X86_AVX2])
            )],
        )
        # Only cn2 should be in our allocation requests now since that's the
        # only one with the required trait
        expected = [
            [('cn2', fields.ResourceClass.VCPU, 1),
             ('cn2', fields.ResourceClass.MEMORY_MB, 64),
             ('cn2', fields.ResourceClass.DISK_GB, 1500)],
        ]
        self._validate_allocation_requests(expected, alloc_cands)
        p_sums = alloc_cands.provider_summaries
        self.assertEqual(1, len(p_sums))

        # And let's verify the provider summary shows the trait
        cn2_p_sum = _find_summary_for_provider(p_sums, cn2.uuid)
        self.assertIsNotNone(cn2_p_sum)
        self.assertEqual(1, len(cn2_p_sum.traits))
        self.assertEqual(os_traits.HW_CPU_X86_AVX2, cn2_p_sum.traits[0].name)

    def test_local_with_shared_disk(self):
        """Create some resource providers that can satisfy the request for
        resources with local VCPU and MEMORY_MB but rely on a shared storage
        pool to satisfy DISK_GB and verify that the allocation requests
        returned by AllocationCandidates have DISK_GB served up by the shared
        storage pool resource provider and VCPU/MEMORY_MB by the compute node
        providers
        """
        # Create two compute node providers with VCPU, RAM and NO local disk,
        # associated with the aggregate.
        cn1, cn2 = (self._create_provider(name, uuids.agg)
                    for name in ('cn1', 'cn2'))
        for cn in (cn1, cn2):
            _add_inventory(cn, fields.ResourceClass.VCPU, 24,
                           allocation_ratio=16.0)
            _add_inventory(cn, fields.ResourceClass.MEMORY_MB, 1024,
                           min_unit=64, allocation_ratio=1.5)

        # Create the shared storage pool, asociated with the same aggregate
        ss = self._create_provider('shared storage', uuids.agg)

        # Give the shared storage pool some inventory of DISK_GB
        _add_inventory(ss, fields.ResourceClass.DISK_GB, 2000, reserved=100,
                       min_unit=10)

        # Mark the shared storage pool as having inventory shared among any
        # provider associated via aggregate
        _set_traits(ss, "MISC_SHARES_VIA_AGGREGATE")

        # Ask for the alternative placement possibilities and verify each
        # compute node provider is listed in the allocation requests as well as
        # the shared storage pool provider
        alloc_cands = self._get_allocation_candidates()

        # Verify the provider summary information indicates 0 usage and
        # capacity calculated from above inventory numbers for both compute
        # nodes
        # TODO(efried): Come up with a terse & readable way to validate
        # provider summaries
        p_sums = alloc_cands.provider_summaries

        self.assertEqual(set([uuids.cn1, uuids.cn2, ss.uuid]),
                         _provider_uuids_from_iterable(p_sums))

        cn1_p_sum = _find_summary_for_provider(p_sums, uuids.cn1)
        self.assertIsNotNone(cn1_p_sum)
        self.assertEqual(2, len(cn1_p_sum.resources))

        cn1_p_sum_vcpu = _find_summary_for_resource(cn1_p_sum, 'VCPU')
        self.assertIsNotNone(cn1_p_sum_vcpu)

        expected_capacity = (24 * 16.0)
        self.assertEqual(expected_capacity, cn1_p_sum_vcpu.capacity)
        self.assertEqual(0, cn1_p_sum_vcpu.used)

        # Let's verify memory for the second compute node
        cn2_p_sum = _find_summary_for_provider(p_sums, uuids.cn2)
        self.assertIsNotNone(cn2_p_sum)
        self.assertEqual(2, len(cn2_p_sum.resources))

        cn2_p_sum_ram = _find_summary_for_resource(cn2_p_sum, 'MEMORY_MB')
        self.assertIsNotNone(cn2_p_sum_ram)

        expected_capacity = (1024 * 1.5)
        self.assertEqual(expected_capacity, cn2_p_sum_ram.capacity)
        self.assertEqual(0, cn2_p_sum_ram.used)

        # Let's verify only diks for the shared storage pool
        ss_p_sum = _find_summary_for_provider(p_sums, ss.uuid)
        self.assertIsNotNone(ss_p_sum)
        self.assertEqual(1, len(ss_p_sum.resources))

        ss_p_sum_disk = _find_summary_for_resource(ss_p_sum, 'DISK_GB')
        self.assertIsNotNone(ss_p_sum_disk)

        expected_capacity = ((2000 - 100) * 1.0)
        self.assertEqual(expected_capacity, ss_p_sum_disk.capacity)
        self.assertEqual(0, ss_p_sum_disk.used)

        # Verify the allocation requests that are returned. There should be 2
        # allocation requests, one for each compute node, containing 3
        # resources in each allocation request, one each for VCPU, RAM, and
        # disk. The amounts of the requests should correspond to the requested
        # resource amounts in the filter:resources dict passed to
        # AllocationCandidates.get_by_requests(). The providers for VCPU and
        # MEMORY_MB should be the compute nodes while the provider for the
        # DISK_GB should be the shared storage pool
        expected = [
            [('cn1', fields.ResourceClass.VCPU, 1),
             ('cn1', fields.ResourceClass.MEMORY_MB, 64),
             ('shared storage', fields.ResourceClass.DISK_GB, 1500)],
            [('cn2', fields.ResourceClass.VCPU, 1),
             ('cn2', fields.ResourceClass.MEMORY_MB, 64),
             ('shared storage', fields.ResourceClass.DISK_GB, 1500)],
        ]
        self._validate_allocation_requests(expected, alloc_cands)

        # Test for bug #1705071. We query for allocation candidates with a
        # request for ONLY the DISK_GB (the resource that is shared with
        # compute nodes) and no VCPU/MEMORY_MB. Before the fix for bug
        # #1705071, this resulted in a KeyError

        alloc_cands = self._get_allocation_candidates(
            requests=[placement_lib.RequestGroup(
                use_same_provider=False,
                resources={
                    'DISK_GB': 10,
                }
            )]
        )

        # We should only have provider summary information for the sharing
        # storage provider, since that's the only provider that can be
        # allocated against for this request.  In the future, we may look into
        # returning the shared-with providers in the provider summaries, but
        # that's a distant possibility.
        self.assertEqual(
            set([ss.uuid]),
            _provider_uuids_from_iterable(alloc_cands.provider_summaries))

        # The allocation_requests will only include the shared storage
        # provider because the only thing we're requesting to allocate is
        # against the provider of DISK_GB, which happens to be the shared
        # storage provider.
        expected = [[('shared storage', fields.ResourceClass.DISK_GB, 10)]]
        self._validate_allocation_requests(expected, alloc_cands)

        # Now we're going to add a set of required traits into the request mix.
        # To start off, let's request a required trait that we know has not
        # been associated yet with any provider, and ensure we get no results
        alloc_cands = self._get_allocation_candidates([
            placement_lib.RequestGroup(
                use_same_provider=False,
                resources=self.requested_resources,
                required_traits=set([os_traits.HW_CPU_X86_AVX2]),
            )],
        )

        # We have not yet associated the AVX2 trait to any provider, so we
        # should get zero allocation candidates
        p_sums = alloc_cands.provider_summaries
        self.assertEqual(0, len(p_sums))

        # Now, if we then associate the required trait with both of our compute
        # nodes, we should get back both compute nodes since they both now
        # satisfy the required traits as well as the resource request
        avx2_t = rp_obj.Trait.get_by_name(self.ctx, os_traits.HW_CPU_X86_AVX2)
        cn1.set_traits([avx2_t])
        cn2.set_traits([avx2_t])

        alloc_cands = self._get_allocation_candidates([
            placement_lib.RequestGroup(
                use_same_provider=False,
                resources=self.requested_resources,
                required_traits=set([os_traits.HW_CPU_X86_AVX2]),
            )],
        )

        p_sums = alloc_cands.provider_summaries
        # There should be 2 compute node providers and 1 shared storage
        # provider in the summaries.
        self.assertEqual(3, len(p_sums))

        self.assertEqual(set([cn1.uuid, cn2.uuid, ss.uuid]),
                         _provider_uuids_from_iterable(p_sums))

        # Let's check that the traits listed for the compute nodes include the
        # AVX2 trait
        cn1_p_sum = _find_summary_for_provider(p_sums, cn1.uuid)
        self.assertIsNotNone(cn1_p_sum)
        self.assertEqual(1, len(cn1_p_sum.traits))
        self.assertEqual(os_traits.HW_CPU_X86_AVX2, cn1_p_sum.traits[0].name)
        cn2_p_sum = _find_summary_for_provider(p_sums, cn2.uuid)
        self.assertIsNotNone(cn2_p_sum)
        self.assertEqual(1, len(cn2_p_sum.traits))
        self.assertEqual(os_traits.HW_CPU_X86_AVX2, cn2_p_sum.traits[0].name)

        # Double-check that the shared storage provider in the provider
        # summaries does NOT have the AVX2 trait
        ss_p_sum = _find_summary_for_provider(p_sums, ss.uuid)
        self.assertIsNotNone(ss_p_sum)
        ss_traits = [trait.name for trait in ss_p_sum.traits]
        self.assertNotIn(os_traits.HW_CPU_X86_AVX2, ss_traits)

    def test_local_with_shared_custom_resource(self):
        """Create some resource providers that can satisfy the request for
        resources with local VCPU and MEMORY_MB but rely on a shared resource
        provider to satisfy a custom resource requirement and verify that the
        allocation requests returned by AllocationCandidates have the custom
        resource served up by the shared custom resource provider and
        VCPU/MEMORY_MB by the compute node providers
        """
        # The aggregate that will be associated to everything...
        agg_uuid = uuids.agg

        # Create two compute node providers with VCPU, RAM and NO local
        # CUSTOM_MAGIC resources, associated with the aggregate.
        for name in ('cn1', 'cn2'):
            cn = self._create_provider(name, agg_uuid)
            _add_inventory(cn, fields.ResourceClass.VCPU, 24,
                           allocation_ratio=16.0)
            _add_inventory(cn, fields.ResourceClass.MEMORY_MB, 1024,
                           min_unit=64, allocation_ratio=1.5)

        # Create a custom resource called MAGIC
        magic_rc = rp_obj.ResourceClass(
            self.ctx,
            name='CUSTOM_MAGIC',
        )
        magic_rc.create()

        # Create the shared provider that serves CUSTOM_MAGIC, associated with
        # the same aggregate
        magic_p = self._create_provider('shared custom resource provider',
                                        agg_uuid)
        _add_inventory(magic_p, magic_rc.name, 2048, reserved=1024,
                       min_unit=10)

        # Mark the magic provider as having inventory shared among any provider
        # associated via aggregate
        _set_traits(magic_p, "MISC_SHARES_VIA_AGGREGATE")

        # The resources we will request
        requested_resources = {
            fields.ResourceClass.VCPU: 1,
            fields.ResourceClass.MEMORY_MB: 64,
            magic_rc.name: 512,
        }

        alloc_cands = self._get_allocation_candidates(
            requests=[placement_lib.RequestGroup(
                use_same_provider=False, resources=requested_resources)])

        # Verify the allocation requests that are returned. There should be 2
        # allocation requests, one for each compute node, containing 3
        # resources in each allocation request, one each for VCPU, RAM, and
        # MAGIC. The amounts of the requests should correspond to the requested
        # resource amounts in the filter:resources dict passed to
        # AllocationCandidates.get_by_requests(). The providers for VCPU and
        # MEMORY_MB should be the compute nodes while the provider for the
        # MAGIC should be the shared custom resource provider.
        expected = [
            [('cn1', fields.ResourceClass.VCPU, 1),
             ('cn1', fields.ResourceClass.MEMORY_MB, 64),
             ('shared custom resource provider', magic_rc.name, 512)],
            [('cn2', fields.ResourceClass.VCPU, 1),
             ('cn2', fields.ResourceClass.MEMORY_MB, 64),
             ('shared custom resource provider', magic_rc.name, 512)],
        ]
        self._validate_allocation_requests(expected, alloc_cands)

    def test_mix_local_and_shared(self):
        # Create three compute node providers with VCPU and RAM, but only
        # the third compute node has DISK. The first two computes will
        # share the storage from the shared storage pool.
        cn1, cn2 = (self._create_provider(name, uuids.agg)
                    for name in ('cn1', 'cn2'))
        # cn3 is not associated with the aggregate
        cn3 = self._create_provider('cn3')
        for cn in (cn1, cn2, cn3):
            _add_inventory(cn, fields.ResourceClass.VCPU, 24,
                           allocation_ratio=16.0)
            _add_inventory(cn, fields.ResourceClass.MEMORY_MB, 1024,
                           min_unit=64, allocation_ratio=1.5)
        # Only cn3 has disk
        _add_inventory(cn3, fields.ResourceClass.DISK_GB, 2000,
                       reserved=100, min_unit=10)

        # Create the shared storage pool in the same aggregate as the first two
        # compute nodes
        ss = self._create_provider('shared storage', uuids.agg)

        # Give the shared storage pool some inventory of DISK_GB
        _add_inventory(ss, fields.ResourceClass.DISK_GB, 2000, reserved=100,
                       min_unit=10)

        _set_traits(ss, "MISC_SHARES_VIA_AGGREGATE")

        alloc_cands = self._get_allocation_candidates()

        # Expect cn1, cn2, cn3 and ss in the summaries
        self.assertEqual(
            set([uuids.cn1, uuids.cn2, ss.uuid, uuids.cn3]),
            _provider_uuids_from_iterable(alloc_cands.provider_summaries))

        # Expect three allocation requests: (cn1, ss), (cn2, ss), (cn3)
        expected = [
            [('cn1', fields.ResourceClass.VCPU, 1),
             ('cn1', fields.ResourceClass.MEMORY_MB, 64),
             ('shared storage', fields.ResourceClass.DISK_GB, 1500)],
            [('cn2', fields.ResourceClass.VCPU, 1),
             ('cn2', fields.ResourceClass.MEMORY_MB, 64),
             ('shared storage', fields.ResourceClass.DISK_GB, 1500)],
            [('cn3', fields.ResourceClass.VCPU, 1),
             ('cn3', fields.ResourceClass.MEMORY_MB, 64),
             ('cn3', fields.ResourceClass.DISK_GB, 1500)],
        ]
        self._validate_allocation_requests(expected, alloc_cands)

        # Now we're going to add a set of required traits into the request mix.
        # To start off, let's request a required trait that we know has not
        # been associated yet with any provider, and ensure we get no results
        alloc_cands = self._get_allocation_candidates([
            placement_lib.RequestGroup(
                use_same_provider=False,
                resources=self.requested_resources,
                required_traits=set([os_traits.HW_CPU_X86_AVX2]),
            )],
        )

        # We have not yet associated the AVX2 trait to any provider, so we
        # should get zero allocation candidates
        p_sums = alloc_cands.provider_summaries
        self.assertEqual(0, len(p_sums))
        a_reqs = alloc_cands.allocation_requests
        self.assertEqual(0, len(a_reqs))

        # Now, if we then associate the required trait with all of our compute
        # nodes, we should get back all compute nodes since they all now
        # satisfy the required traits as well as the resource request
        for cn in (cn1, cn2, cn3):
            _set_traits(cn, os_traits.HW_CPU_X86_AVX2)

        alloc_cands = self._get_allocation_candidates([
            placement_lib.RequestGroup(
                use_same_provider=False,
                resources=self.requested_resources,
                required_traits=set([os_traits.HW_CPU_X86_AVX2]),
            )],
        )

        p_sums = alloc_cands.provider_summaries
        # There should be 3 compute node providers and 1 shared storage
        # provider in the summaries.
        self.assertEqual(4, len(p_sums))
        expected_prov_uuids = set([cn1.uuid, cn2.uuid, cn3.uuid, ss.uuid])
        self.assertEqual(expected_prov_uuids,
                         _provider_uuids_from_iterable(p_sums))

        # Let's check that the traits listed for the compute nodes include the
        # AVX2 trait
        cn1_p_sum = _find_summary_for_provider(p_sums, cn1.uuid)
        self.assertIsNotNone(cn1_p_sum)
        self.assertEqual(1, len(cn1_p_sum.traits))
        self.assertEqual(os_traits.HW_CPU_X86_AVX2, cn1_p_sum.traits[0].name)
        cn2_p_sum = _find_summary_for_provider(p_sums, cn2.uuid)
        self.assertIsNotNone(cn2_p_sum)
        self.assertEqual(1, len(cn2_p_sum.traits))
        self.assertEqual(os_traits.HW_CPU_X86_AVX2, cn2_p_sum.traits[0].name)
        cn3_p_sum = _find_summary_for_provider(p_sums, cn3.uuid)
        self.assertIsNotNone(cn3_p_sum)
        self.assertEqual(1, len(cn3_p_sum.traits))
        self.assertEqual(os_traits.HW_CPU_X86_AVX2, cn3_p_sum.traits[0].name)

        # Double-check that the shared storage provider in the provider
        # summaries does NOT have the AVX2 trait
        ss_p_sum = _find_summary_for_provider(p_sums, ss.uuid)
        self.assertIsNotNone(ss_p_sum)
        ss_traits = [trait.name for trait in ss_p_sum.traits]
        self.assertNotIn(os_traits.HW_CPU_X86_AVX2, ss_traits)

        # We should have a total of 3 allocation requests, representing
        # potential claims against cn1 and cn2 with shared storage and against
        # cn3 with all resources
        a_reqs = alloc_cands.allocation_requests
        self.assertEqual(3, len(a_reqs))

        # Now, let's add a new wrinkle to the equation and add a required trait
        # that will ONLY be satisfied by a compute node with local disk that
        # has SSD drives. Set this trait only on the compute node with local
        # disk (cn3)
        _set_traits(cn3, os_traits.HW_CPU_X86_AVX2, os_traits.STORAGE_DISK_SSD)

        alloc_cands = self._get_allocation_candidates([
            placement_lib.RequestGroup(
                use_same_provider=False,
                resources=self.requested_resources,
                required_traits=set([
                    os_traits.HW_CPU_X86_AVX2, os_traits.STORAGE_DISK_SSD
                ]),
            )],
        )

        p_sums = alloc_cands.provider_summaries
        # There should be only cn3 in the returned provider summaries
        self.assertEqual(1, len(p_sums))
        expected_prov_uuids = set([cn3.uuid])
        self.assertEqual(expected_prov_uuids,
                         _provider_uuids_from_iterable(p_sums))

    def test_common_rc(self):
        """Candidates when cn and shared have inventory in the same class."""
        cn = self._create_provider('cn', uuids.agg1)
        _add_inventory(cn, fields.ResourceClass.VCPU, 24)
        _add_inventory(cn, fields.ResourceClass.MEMORY_MB, 2048)
        _add_inventory(cn, fields.ResourceClass.DISK_GB, 1600)

        ss = self._create_provider('ss', uuids.agg1)
        _set_traits(ss, "MISC_SHARES_VIA_AGGREGATE")
        _add_inventory(ss, fields.ResourceClass.DISK_GB, 1600)

        alloc_cands = self._get_allocation_candidates()

        # One allocation_request should have cn + ss; the other should have
        # just the cn.
        expected = [
            [('cn', fields.ResourceClass.VCPU, 1),
             ('cn', fields.ResourceClass.MEMORY_MB, 64),
             ('cn', fields.ResourceClass.DISK_GB, 1500)],
            # TODO(efried): Due to bug #1724613, the cn + ss candidate is not
            # returned.  Uncomment this when the bug is fixed.
            # [('cn', fields.ResourceClass.VCPU, 1),
            #  ('cn', fields.ResourceClass.MEMORY_MB, 64),
            #  ('ss', fields.ResourceClass.DISK_GB, 1500)],
        ]

        self._validate_allocation_requests(expected, alloc_cands)

    def test_common_rc_traits_split(self):
        """Validate filters when traits are split across cn and shared RPs."""
        # NOTE(efried): This test case only applies to the scenario where we're
        # requesting resources via the RequestGroup where
        # use_same_provider=False

        cn = self._create_provider('cn', uuids.agg1)
        _add_inventory(cn, fields.ResourceClass.VCPU, 24)
        _add_inventory(cn, fields.ResourceClass.MEMORY_MB, 2048)
        _add_inventory(cn, fields.ResourceClass.DISK_GB, 1600)
        # The compute node's disk is SSD
        _set_traits(cn, 'HW_CPU_X86_SSE', 'STORAGE_DISK_SSD')

        ss = self._create_provider('ss', uuids.agg1)
        _add_inventory(ss, fields.ResourceClass.DISK_GB, 1600)
        # The shared storage's disk is RAID
        _set_traits(ss, 'MISC_SHARES_VIA_AGGREGATE', 'CUSTOM_RAID')

        alloc_cands = rp_obj.AllocationCandidates.get_by_requests(
            self.ctx, [
                placement_lib.RequestGroup(
                    use_same_provider=False,
                    resources=self.requested_resources,
                    required_traits=set(['HW_CPU_X86_SSE', 'STORAGE_DISK_SSD',
                                         'CUSTOM_RAID'])
                )
            ]
        )

        # TODO(efried): Bug #1724633: we'd *like* to get no candidates, because
        # there's no single DISK_GB resource with both STORAGE_DISK_SSD and
        # CUSTOM_RAID traits.  So this is the ideal expected value:
        expected = []
        # TODO(efried): But under the design as currently conceived, we would
        # expect to get the cn + ss candidate, because that combination
        # satisfies both traits:
        # expected = [
        #     [('cn', fields.ResourceClass.VCPU, 1),
        #      ('cn', fields.ResourceClass.MEMORY_MB, 64),
        #      ('ss', fields.ResourceClass.DISK_GB, 1500)],
        # ]
        # So we're getting the right value, but we really shouldn't be.
        self._validate_allocation_requests(expected, alloc_cands)

    def test_only_one_sharing_provider(self):
        ss1 = self._create_provider('ss1', uuids.agg1)
        _set_traits(ss1, "MISC_SHARES_VIA_AGGREGATE")
        _add_inventory(ss1, fields.ResourceClass.IPV4_ADDRESS, 24)
        _add_inventory(ss1, fields.ResourceClass.SRIOV_NET_VF, 16)
        _add_inventory(ss1, fields.ResourceClass.DISK_GB, 1600)

        alloc_cands = self._get_allocation_candidates([
            placement_lib.RequestGroup(
                use_same_provider=False,
                resources={
                    'IPV4_ADDRESS': 2,
                    'SRIOV_NET_VF': 1,
                    'DISK_GB': 1500,
                }
            )]
        )

        expected = [
            [('ss1', fields.ResourceClass.IPV4_ADDRESS, 2),
             ('ss1', fields.ResourceClass.SRIOV_NET_VF, 1),
             ('ss1', fields.ResourceClass.DISK_GB, 1500)]
        ]
        self._validate_allocation_requests(expected, alloc_cands)

    def test_all_sharing_providers_no_rc_overlap(self):
        ss1 = self._create_provider('ss1', uuids.agg1)
        _set_traits(ss1, "MISC_SHARES_VIA_AGGREGATE")
        _add_inventory(ss1, fields.ResourceClass.IPV4_ADDRESS, 24)

        ss2 = self._create_provider('ss2', uuids.agg1)
        _set_traits(ss2, "MISC_SHARES_VIA_AGGREGATE")
        _add_inventory(ss2, fields.ResourceClass.DISK_GB, 1600)

        alloc_cands = self._get_allocation_candidates([
            placement_lib.RequestGroup(
                use_same_provider=False,
                resources={
                    'IPV4_ADDRESS': 2,
                    'DISK_GB': 1500,
                }
            )]
        )

        # TODO(gibi): Bug https://bugs.launchpad.net/nova/+bug/1730730
        # We expect only one candidate where IPV4_ADDRESS comes from ss1 and
        # DISK_GB comes from ss2
        # expected = [
        #     [('ss1', fields.ResourceClass.IPV4_ADDRESS, 2),
        #      ('ss2', fields.ResourceClass.DISK_GB, 1500)]
        # ]
        # But we are getting the same candidate twice
        expected = [
            # this is what we expect but then
            [('ss1', fields.ResourceClass.IPV4_ADDRESS, 2),
             ('ss2', fields.ResourceClass.DISK_GB, 1500)],
            # we get the same thing again
            [('ss1', fields.ResourceClass.IPV4_ADDRESS, 2),
             ('ss2', fields.ResourceClass.DISK_GB, 1500)],
        ]
        self._validate_allocation_requests(expected, alloc_cands)

    def test_all_sharing_providers_no_rc_overlap_more_classes(self):
        ss1 = self._create_provider('ss1', uuids.agg1)
        _set_traits(ss1, "MISC_SHARES_VIA_AGGREGATE")
        _add_inventory(ss1, fields.ResourceClass.IPV4_ADDRESS, 24)
        _add_inventory(ss1, fields.ResourceClass.SRIOV_NET_VF, 16)

        ss2 = self._create_provider('ss2', uuids.agg1)
        _set_traits(ss2, "MISC_SHARES_VIA_AGGREGATE")
        _add_inventory(ss2, fields.ResourceClass.DISK_GB, 1600)

        alloc_cands = self._get_allocation_candidates([
            placement_lib.RequestGroup(
                use_same_provider=False,
                resources={
                    'IPV4_ADDRESS': 2,
                    'SRIOV_NET_VF': 1,
                    'DISK_GB': 1500,
                }
            )]
        )

        # TODO(gibi/efried): Bug https://bugs.launchpad.net/nova/+bug/1730730
        # We expect only one candidate where IPV4_ADDRESS and SRIOV_NET_VF
        # comes from ss1 and DISK_GB comes from ss2
        # expected = [
        #     [('ss1', fields.ResourceClass.IPV4_ADDRESS, 2),
        #      ('ss1', fields.ResourceClass.SRIOV_NET_VF, 1),
        #      ('ss2', fields.ResourceClass.DISK_GB, 1500)]
        # ]
        # But we're actually seeing the expected candidate twice
        expected = [
            [('ss1', fields.ResourceClass.IPV4_ADDRESS, 2),
             ('ss1', fields.ResourceClass.SRIOV_NET_VF, 1),
             ('ss2', fields.ResourceClass.DISK_GB, 1500)],
            [('ss1', fields.ResourceClass.IPV4_ADDRESS, 2),
             ('ss1', fields.ResourceClass.SRIOV_NET_VF, 1),
             ('ss2', fields.ResourceClass.DISK_GB, 1500)],
        ]
        self._validate_allocation_requests(expected, alloc_cands)

    def test_all_sharing_providers(self):
        ss1 = self._create_provider('ss1', uuids.agg1)
        _set_traits(ss1, "MISC_SHARES_VIA_AGGREGATE")
        _add_inventory(ss1, fields.ResourceClass.IPV4_ADDRESS, 24)
        _add_inventory(ss1, fields.ResourceClass.SRIOV_NET_VF, 16)
        _add_inventory(ss1, fields.ResourceClass.DISK_GB, 1600)

        ss2 = self._create_provider('ss2', uuids.agg1)
        _set_traits(ss2, "MISC_SHARES_VIA_AGGREGATE")
        _add_inventory(ss2, fields.ResourceClass.DISK_GB, 1600)

        alloc_cands = self._get_allocation_candidates(requests=[
            placement_lib.RequestGroup(
                use_same_provider=False,
                resources={
                    'IPV4_ADDRESS': 2,
                    'SRIOV_NET_VF': 1,
                    'DISK_GB': 1500,
                }
            )]
        )

        # We expect two candidates: one that gets all the resources from ss1;
        # and one that gets the DISK_GB from ss2 and the rest from ss1:
        expected = [
            [('ss1', fields.ResourceClass.IPV4_ADDRESS, 2),
             ('ss1', fields.ResourceClass.SRIOV_NET_VF, 1),
             ('ss1', fields.ResourceClass.DISK_GB, 1500)],
            [('ss1', fields.ResourceClass.IPV4_ADDRESS, 2),
             ('ss1', fields.ResourceClass.SRIOV_NET_VF, 1),
             ('ss2', fields.ResourceClass.DISK_GB, 1500)],
        ]
        self._validate_allocation_requests(expected, alloc_cands)

    def test_two_non_sharing_connect_to_one_sharing_different_aggregate(self):
        # Covering the following setup:
        #
        #    CN1 (VCPU)        CN2 (VCPU)
        #        \ agg1        / agg2
        #         SS1 (DISK_GB)
        #
        # It is different from test_mix_local_and_shared as it uses two
        # different aggregates to connect the two CNs to the share RP

        cn1 = self._create_provider('cn1', uuids.agg1)
        _add_inventory(cn1, fields.ResourceClass.VCPU, 24)
        _add_inventory(cn1, fields.ResourceClass.MEMORY_MB, 2048)

        cn2 = self._create_provider('cn2', uuids.agg2)
        _add_inventory(cn2, fields.ResourceClass.VCPU, 24)
        _add_inventory(cn2, fields.ResourceClass.MEMORY_MB, 2048)

        ss1 = self._create_provider('ss1', uuids.agg1, uuids.agg2)
        _set_traits(ss1, "MISC_SHARES_VIA_AGGREGATE")
        _add_inventory(ss1, fields.ResourceClass.DISK_GB, 1600)

        alloc_cands = self._get_allocation_candidates([
            placement_lib.RequestGroup(
                use_same_provider=False,
                resources={
                    'VCPU': 2,
                    'DISK_GB': 1500,
                }
            )]
        )
        expected = [
            [('cn1', fields.ResourceClass.VCPU, 2),
             ('ss1', fields.ResourceClass.DISK_GB, 1500)],
            [('cn2', fields.ResourceClass.VCPU, 2),
             ('ss1', fields.ResourceClass.DISK_GB, 1500)],
        ]
        self._validate_allocation_requests(expected, alloc_cands)

    def test_two_non_sharing_one_common_and_two_unique_sharing(self):
        # Covering the following setup:
        #
        #    CN1 (VCPU)          CN2 (VCPU)
        #   / agg3   \ agg1     / agg1   \ agg2
        #  SS3 (IPV4)   SS1 (DISK_GB)      SS2 (IPV4)

        cn1 = self._create_provider('cn1', uuids.agg1, uuids.agg3)
        _add_inventory(cn1, fields.ResourceClass.VCPU, 24)
        _add_inventory(cn1, fields.ResourceClass.MEMORY_MB, 2048)

        cn2 = self._create_provider('cn2', uuids.agg1, uuids.agg2)
        _add_inventory(cn2, fields.ResourceClass.VCPU, 24)
        _add_inventory(cn2, fields.ResourceClass.MEMORY_MB, 2048)

        # ss1 is connected to both cn1 and cn2
        ss1 = self._create_provider('ss1', uuids.agg1)
        _set_traits(ss1, "MISC_SHARES_VIA_AGGREGATE")
        _add_inventory(ss1, fields.ResourceClass.DISK_GB, 1600)

        # ss2 only connected to cn2
        ss2 = self._create_provider('ss2', uuids.agg2)
        _set_traits(ss2, "MISC_SHARES_VIA_AGGREGATE")
        _add_inventory(ss2, fields.ResourceClass.IPV4_ADDRESS, 24)

        # ss3 only connected to cn1
        ss3 = self._create_provider('ss3', uuids.agg3)
        _set_traits(ss3, "MISC_SHARES_VIA_AGGREGATE")
        _add_inventory(ss3, fields.ResourceClass.IPV4_ADDRESS, 24)

        alloc_cands = self._get_allocation_candidates([
            placement_lib.RequestGroup(
                use_same_provider=False,
                resources={
                    'VCPU': 2,
                    'DISK_GB': 1500,
                    'IPV4_ADDRESS': 2,
                }
            )]
        )
        # NOTE(gibi): We expect two candidates one with cn1, ss1 and ss3
        # and one with cn2, ss1 and ss2 but we get two invalid combinations as
        # well. This is reported in bug 1732707
        expected = [
            [('cn1', fields.ResourceClass.VCPU, 2),
             ('ss1', fields.ResourceClass.DISK_GB, 1500),
             ('ss3', fields.ResourceClass.IPV4_ADDRESS, 2)],
            [('cn2', fields.ResourceClass.VCPU, 2),
             ('ss1', fields.ResourceClass.DISK_GB, 1500),
             ('ss2', fields.ResourceClass.IPV4_ADDRESS, 2)],
            # 1) cn1 and ss2 are not connected so this is not valid
            [('cn1', fields.ResourceClass.VCPU, 2),
             ('ss1', fields.ResourceClass.DISK_GB, 1500),
             ('ss2', fields.ResourceClass.IPV4_ADDRESS, 2)],
            # 2) cn2 and ss3 are not connected so this is not valid
            [('cn2', fields.ResourceClass.VCPU, 2),
             ('ss1', fields.ResourceClass.DISK_GB, 1500),
             ('ss3', fields.ResourceClass.IPV4_ADDRESS, 2)],
        ]

        self._validate_allocation_requests(expected, alloc_cands)
