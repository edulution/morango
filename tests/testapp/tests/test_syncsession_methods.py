import json
import uuid

import factory
from django.db import connection
from django.test import TestCase
from django.test import override_settings
from django.utils import timezone
from facility_profile.models import Facility
import mock

from .helpers import create_buffer_and_store_dummy_data
from .helpers import create_dummy_store_data
from morango.constants import transfer_status
from morango.models.core import Buffer
from morango.models.core import DatabaseIDModel
from morango.models.core import DatabaseMaxCounter
from morango.models.core import InstanceIDModel
from morango.models.core import RecordMaxCounter
from morango.models.core import RecordMaxCounterBuffer
from morango.models.core import Store
from morango.models.core import SyncSession
from morango.models.core import TransferSession
from morango.sync.backends.utils import load_backend
from morango.sync.controller import LocalSessionContext
from morango.sync.controller import MorangoProfileController
from morango.sync.operations import _dequeue_into_store
from morango.sync.operations import _queue_into_buffer
from morango.sync.operations import LocalQueueOperation
from morango.sync.operations import LocalDequeueOperation
from morango.sync.syncsession import TransferClient
from morango.sync.syncsession import SyncClientSignals
from morango.sync.syncsession import SyncSignal
from morango.sync.syncsession import SyncSignalGroup

DBBackend = load_backend(connection).SQLWrapper()


class FacilityModelFactory(factory.DjangoModelFactory):
    class Meta:
        model = Facility

    name = factory.Sequence(lambda n: "Fac %d" % n)


@override_settings(MORANGO_SERIALIZE_BEFORE_QUEUING=False)
class QueueStoreIntoBufferTestCase(TestCase):
    def setUp(self):
        super(QueueStoreIntoBufferTestCase, self).setUp()
        self.data = create_dummy_store_data()
        self.transfer_session = self.data["sc"].current_transfer_session
        self.context = mock.Mock(
            spec=LocalSessionContext,
            transfer_session=self.transfer_session,
            sync_session=self.transfer_session.sync_session,
            is_server=False,
        )

    def assertRecordsBuffered(self, records):
        buffer_ids = Buffer.objects.values_list("model_uuid", flat=True)
        rmcb_ids = RecordMaxCounterBuffer.objects.values_list("model_uuid", flat=True)
        # ensure all store and buffer records are buffered
        for i in records:
            self.assertIn(i.id, buffer_ids)
            self.assertIn(i.id, rmcb_ids)

    def assertRecordsNotBuffered(self, records):
        buffer_ids = Buffer.objects.values_list("model_uuid", flat=True)
        rmcb_ids = RecordMaxCounterBuffer.objects.values_list("model_uuid", flat=True)
        # ensure all store and buffer records are buffered
        for i in records:
            self.assertNotIn(i.id, buffer_ids)
            self.assertNotIn(i.id, rmcb_ids)

    def test_all_fsics(self):
        fsics = {self.data["group1_id"].id: 1, self.data["group2_id"].id: 1}
        self.transfer_session.client_fsic = json.dumps(fsics)
        _queue_into_buffer(self.transfer_session)
        # ensure all store and buffer records are buffered
        self.assertRecordsBuffered(self.data["group1_c1"])
        self.assertRecordsBuffered(self.data["group1_c2"])
        self.assertRecordsBuffered(self.data["group2_c1"])

    def test_fsic_specific_id(self):
        fsics = {self.data["group2_id"].id: 1}
        self.transfer_session.client_fsic = json.dumps(fsics)
        _queue_into_buffer(self.transfer_session)
        # ensure only records modified with 2nd instance id are buffered
        self.assertRecordsNotBuffered(self.data["group1_c1"])
        self.assertRecordsNotBuffered(self.data["group1_c2"])
        self.assertRecordsBuffered(self.data["group2_c1"])

    def test_fsic_counters(self):
        counter = InstanceIDModel.objects.get(id=self.data["group1_id"].id).counter
        fsics = {self.data["group1_id"].id: counter - 1}
        self.transfer_session.client_fsic = json.dumps(fsics)
        fsics[self.data["group1_id"].id] = 0
        self.transfer_session.server_fsic = json.dumps(fsics)
        _queue_into_buffer(self.transfer_session)
        # ensure only records with updated 1st instance id are buffered
        self.assertRecordsBuffered(self.data["group1_c1"])
        self.assertRecordsBuffered(self.data["group1_c2"])
        self.assertRecordsNotBuffered(self.data["group2_c1"])

    def test_fsic_counters_too_high(self):
        fsics = {self.data["group1_id"].id: 100, self.data["group2_id"].id: 100}
        self.transfer_session.client_fsic = json.dumps(fsics)
        self.transfer_session.server_fsic = json.dumps(fsics)
        _queue_into_buffer(self.transfer_session)
        # ensure no records are buffered
        self.assertFalse(Buffer.objects.all())
        self.assertFalse(RecordMaxCounterBuffer.objects.all())

    def test_partition_filter_buffering(self):
        fsics = {self.data["group2_id"].id: 1}
        filter_prefixes = "{}:user:summary\n{}:user:interaction".format(
            self.data["user3"].id, self.data["user3"].id
        )
        self.transfer_session.filter = filter_prefixes
        self.transfer_session.client_fsic = json.dumps(fsics)
        _queue_into_buffer(self.transfer_session)
        # ensure records with different partition values are buffered
        self.assertRecordsNotBuffered([self.data["user2"]])
        self.assertRecordsBuffered(self.data["user3_sumlogs"])
        self.assertRecordsBuffered(self.data["user3_interlogs"])

    def test_partition_prefix_buffering(self):
        fsics = {self.data["group2_id"].id: 1}
        filter_prefixes = "{}".format(self.data["user2"].id)
        self.transfer_session.filter = filter_prefixes
        self.transfer_session.client_fsic = json.dumps(fsics)
        _queue_into_buffer(self.transfer_session)
        # ensure only records with user2 partition are buffered
        self.assertRecordsBuffered([self.data["user2"]])
        self.assertRecordsBuffered(self.data["user2_sumlogs"])
        self.assertRecordsBuffered(self.data["user2_interlogs"])
        self.assertRecordsNotBuffered([self.data["user3"]])

    def test_partition_and_fsic_buffering(self):
        filter_prefixes = "{}:user:summary".format(self.data["user1"].id)
        fsics = {self.data["group1_id"].id: 1}
        self.transfer_session.filter = filter_prefixes
        self.transfer_session.client_fsic = json.dumps(fsics)
        _queue_into_buffer(self.transfer_session)
        # ensure records updated with 1st instance id and summarylog partition are buffered
        self.assertRecordsBuffered(self.data["user1_sumlogs"])
        self.assertRecordsNotBuffered(self.data["user2_sumlogs"])
        self.assertRecordsNotBuffered(self.data["user3_sumlogs"])

    def test_valid_fsic_but_invalid_partition(self):
        filter_prefixes = "{}:user:summary".format(self.data["user1"].id)
        fsics = {self.data["group2_id"].id: 1}
        self.transfer_session.filter = filter_prefixes
        self.transfer_session.client_fsic = json.dumps(fsics)
        _queue_into_buffer(self.transfer_session)
        # ensure that record with valid fsic but invalid partition is not buffered
        self.assertRecordsNotBuffered([self.data["user4"]])

    def test_local_queue_operation(self):
        fsics = {self.data["group1_id"].id: 1, self.data["group2_id"].id: 1}
        self.transfer_session.client_fsic = json.dumps(fsics)

        self.assertEqual(0, self.transfer_session.records_total or 0)
        operation = LocalQueueOperation()
        self.assertEqual(transfer_status.COMPLETED, operation.handle(self.context))
        self.assertNotEqual(0, self.transfer_session.records_total)

        # ensure all store and buffer records are buffered
        self.assertRecordsBuffered(self.data["group1_c1"])
        self.assertRecordsBuffered(self.data["group1_c2"])
        self.assertRecordsBuffered(self.data["group2_c1"])

    @mock.patch("morango.sync.operations._queue_into_buffer")
    def test_local_queue_operation__noop(self, mock_queue):
        fsics = {self.data["group1_id"].id: 1, self.data["group2_id"].id: 1}
        self.transfer_session.client_fsic = json.dumps(fsics)

        # as server, for push, operation should not queue into buffer
        self.context.is_server = True

        operation = LocalQueueOperation()
        self.assertEqual(transfer_status.COMPLETED, operation.handle(self.context))
        mock_queue.assert_not_called()


@override_settings(MORANGO_DESERIALIZE_AFTER_DEQUEUING=False)
class BufferIntoStoreTestCase(TestCase):
    def setUp(self):
        super(BufferIntoStoreTestCase, self).setUp()
        self.data = {}
        DatabaseIDModel.objects.create()
        (self.current_id, _) = InstanceIDModel.get_or_create_current_instance()

        # create controllers for app/store/buffer operations
        conn = mock.Mock(spec="morango.sync.syncsession.NetworkSyncConnection")
        conn.server_info = dict(capabilities=[])
        self.data["mc"] = MorangoProfileController("facilitydata")
        self.data["sc"] = TransferClient(conn, "host")
        session = SyncSession.objects.create(
            id=uuid.uuid4().hex, profile="", last_activity_timestamp=timezone.now()
        )
        self.data["sc"].current_transfer_session = TransferSession.objects.create(
            id=uuid.uuid4().hex,
            sync_session=session,
            push=True,
            last_activity_timestamp=timezone.now(),
        )
        self.transfer_session = self.data["sc"].current_transfer_session
        self.data.update(
            create_buffer_and_store_dummy_data(
                self.data["sc"].current_transfer_session.id
            )
        )
        self.context = mock.Mock(
            spec=LocalSessionContext,
            transfer_session=self.transfer_session,
            sync_session=self.transfer_session.sync_session,
            is_server=True,
        )

    def test_dequeuing_delete_rmcb_records(self):
        for i in self.data["model1_rmcb_ids"]:
            self.assertTrue(
                RecordMaxCounterBuffer.objects.filter(
                    instance_id=i, model_uuid=self.data["model1"]
                ).exists()
            )
        with connection.cursor() as cursor:
            DBBackend._dequeuing_delete_rmcb_records(cursor, self.transfer_session.id)
        for i in self.data["model1_rmcb_ids"]:
            self.assertFalse(
                RecordMaxCounterBuffer.objects.filter(
                    instance_id=i, model_uuid=self.data["model1"]
                ).exists()
            )
        # ensure other records were not deleted
        for i in self.data["model2_rmcb_ids"]:
            self.assertTrue(
                RecordMaxCounterBuffer.objects.filter(
                    instance_id=i, model_uuid=self.data["model2"]
                ).exists()
            )

    def test_dequeuing_delete_buffered_records(self):
        self.assertTrue(Buffer.objects.filter(model_uuid=self.data["model1"]).exists())
        with connection.cursor() as cursor:
            DBBackend._dequeuing_delete_buffered_records(
                cursor, self.transfer_session.id
            )
        self.assertFalse(Buffer.objects.filter(model_uuid=self.data["model1"]).exists())
        # ensure other records were not deleted
        self.assertTrue(Buffer.objects.filter(model_uuid=self.data["model2"]).exists())

    def test_dequeuing_merge_conflict_rmcb_greater_than_rmc(self):
        rmc = RecordMaxCounter.objects.get(
            instance_id=self.data["model2_rmc_ids"][0],
            store_model_id=self.data["model2"],
        )
        rmcb = RecordMaxCounterBuffer.objects.get(
            instance_id=self.data["model2_rmc_ids"][0], model_uuid=self.data["model2"]
        )
        self.assertNotEqual(rmc.counter, rmcb.counter)
        self.assertGreaterEqual(rmcb.counter, rmc.counter)
        with connection.cursor() as cursor:
            DBBackend._dequeuing_merge_conflict_rmcb(cursor, self.transfer_session.id)
        rmc = RecordMaxCounter.objects.get(
            instance_id=self.data["model2_rmc_ids"][0],
            store_model_id=self.data["model2"],
        )
        rmcb = RecordMaxCounterBuffer.objects.get(
            instance_id=self.data["model2_rmc_ids"][0], model_uuid=self.data["model2"]
        )
        self.assertEqual(rmc.counter, rmcb.counter)

    def test_dequeuing_merge_conflict_rmcb_less_than_rmc(self):
        rmc = RecordMaxCounter.objects.get(
            instance_id=self.data["model5_rmc_ids"][0],
            store_model_id=self.data["model5"],
        )
        rmcb = RecordMaxCounterBuffer.objects.get(
            instance_id=self.data["model5_rmc_ids"][0], model_uuid=self.data["model5"]
        )
        self.assertNotEqual(rmc.counter, rmcb.counter)
        self.assertGreaterEqual(rmc.counter, rmcb.counter)
        with connection.cursor() as cursor:
            DBBackend._dequeuing_merge_conflict_rmcb(cursor, self.transfer_session.id)
        rmc = RecordMaxCounter.objects.get(
            instance_id=self.data["model5_rmc_ids"][0],
            store_model_id=self.data["model5"],
        )
        rmcb = RecordMaxCounterBuffer.objects.get(
            instance_id=self.data["model5_rmc_ids"][0], model_uuid=self.data["model5"]
        )
        self.assertNotEqual(rmc.counter, rmcb.counter)
        self.assertGreaterEqual(rmc.counter, rmcb.counter)

    def test_dequeuing_merge_conflict_buffer_rmcb_greater_than_rmc(self):
        store = Store.objects.get(id=self.data["model2"])
        self.assertNotEqual(store.last_saved_instance, self.current_id.id)
        self.assertEqual(store.conflicting_serialized_data, "store")
        self.assertFalse(store.deleted)
        with connection.cursor() as cursor:
            current_id = InstanceIDModel.get_current_instance_and_increment_counter()
            DBBackend._dequeuing_merge_conflict_buffer(
                cursor, current_id, self.transfer_session.id
            )
        store = Store.objects.get(id=self.data["model2"])
        self.assertEqual(store.last_saved_instance, current_id.id)
        self.assertEqual(store.last_saved_counter, current_id.counter)
        self.assertEqual(store.conflicting_serialized_data, "buffer\nstore")
        self.assertTrue(store.deleted)

    def test_dequeuing_merge_conflict_buffer_rmcb_less_rmc(self):
        store = Store.objects.get(id=self.data["model5"])
        self.assertNotEqual(store.last_saved_instance, self.current_id.id)
        self.assertEqual(store.conflicting_serialized_data, "store")
        with connection.cursor() as cursor:
            current_id = InstanceIDModel.get_current_instance_and_increment_counter()
            DBBackend._dequeuing_merge_conflict_buffer(
                cursor, current_id, self.transfer_session.id
            )
        store = Store.objects.get(id=self.data["model5"])
        self.assertEqual(store.last_saved_instance, current_id.id)
        self.assertEqual(store.last_saved_counter, current_id.counter)
        self.assertEqual(store.conflicting_serialized_data, "buffer\nstore")

    def test_dequeuing_merge_conflict_hard_delete(self):
        store = Store.objects.get(id=self.data["model7"])
        self.assertEqual(store.serialized, "store")
        self.assertEqual(store.conflicting_serialized_data, "store")
        with connection.cursor() as cursor:
            current_id = InstanceIDModel.get_current_instance_and_increment_counter()
            DBBackend._dequeuing_merge_conflict_buffer(
                cursor, current_id, self.transfer_session.id
            )
        store.refresh_from_db()
        self.assertEqual(store.serialized, "")
        self.assertEqual(store.conflicting_serialized_data, "")

    def test_dequeuing_update_rmcs_last_saved_by(self):
        self.assertFalse(
            RecordMaxCounter.objects.filter(instance_id=self.current_id.id).exists()
        )
        with connection.cursor() as cursor:
            current_id = InstanceIDModel.get_current_instance_and_increment_counter()
            DBBackend._dequeuing_update_rmcs_last_saved_by(
                cursor, current_id, self.transfer_session.id
            )
        self.assertTrue(
            RecordMaxCounter.objects.filter(instance_id=current_id.id).exists()
        )

    def test_dequeuing_delete_mc_buffer(self):
        self.assertTrue(Buffer.objects.filter(model_uuid=self.data["model2"]).exists())
        with connection.cursor() as cursor:
            DBBackend._dequeuing_delete_mc_buffer(cursor, self.transfer_session.id)
        self.assertFalse(Buffer.objects.filter(model_uuid=self.data["model2"]).exists())
        # ensure other records were not deleted
        self.assertTrue(Buffer.objects.filter(model_uuid=self.data["model3"]).exists())

    def test_dequeuing_delete_mc_rmcb(self):
        self.assertTrue(
            RecordMaxCounterBuffer.objects.filter(
                model_uuid=self.data["model2"],
                instance_id=self.data["model2_rmcb_ids"][0],
            ).exists()
        )
        with connection.cursor() as cursor:
            DBBackend._dequeuing_delete_mc_rmcb(cursor, self.transfer_session.id)
        self.assertFalse(
            RecordMaxCounterBuffer.objects.filter(
                model_uuid=self.data["model2"],
                instance_id=self.data["model2_rmcb_ids"][0],
            ).exists()
        )
        self.assertTrue(
            RecordMaxCounterBuffer.objects.filter(
                model_uuid=self.data["model2"],
                instance_id=self.data["model2_rmcb_ids"][1],
            ).exists()
        )
        # ensure other records were not deleted
        self.assertTrue(
            RecordMaxCounterBuffer.objects.filter(
                model_uuid=self.data["model3"],
                instance_id=self.data["model3_rmcb_ids"][0],
            ).exists()
        )

    def test_dequeuing_insert_remaining_buffer(self):
        self.assertNotEqual(
            Store.objects.get(id=self.data["model3"]).serialized, "buffer"
        )
        self.assertFalse(Store.objects.filter(id=self.data["model4"]).exists())
        with connection.cursor() as cursor:
            DBBackend._dequeuing_insert_remaining_buffer(
                cursor, self.transfer_session.id
            )
        self.assertEqual(Store.objects.get(id=self.data["model3"]).serialized, "buffer")
        self.assertTrue(Store.objects.filter(id=self.data["model4"]).exists())

    def test_dequeuing_insert_remaining_rmcb(self):
        for i in self.data["model4_rmcb_ids"]:
            self.assertFalse(
                RecordMaxCounter.objects.filter(
                    instance_id=i, store_model_id=self.data["model4"]
                ).exists()
            )
        with connection.cursor() as cursor:
            DBBackend._dequeuing_insert_remaining_buffer(
                cursor, self.transfer_session.id
            )
            DBBackend._dequeuing_insert_remaining_rmcb(cursor, self.transfer_session.id)
        for i in self.data["model4_rmcb_ids"]:
            self.assertTrue(
                RecordMaxCounter.objects.filter(
                    instance_id=i, store_model_id=self.data["model4"]
                ).exists()
            )

    def test_dequeuing_delete_remaining_rmcb(self):
        self.assertTrue(
            RecordMaxCounterBuffer.objects.filter(
                transfer_session_id=self.transfer_session.id
            ).exists()
        )
        with connection.cursor() as cursor:
            DBBackend._dequeuing_delete_remaining_rmcb(cursor, self.transfer_session.id)
        self.assertFalse(
            RecordMaxCounterBuffer.objects.filter(
                transfer_session_id=self.transfer_session.id
            ).exists()
        )

    def test_dequeuing_delete_remaining_buffer(self):
        self.assertTrue(
            Buffer.objects.filter(transfer_session_id=self.transfer_session.id).exists()
        )
        with connection.cursor() as cursor:
            DBBackend._dequeuing_delete_remaining_buffer(
                cursor, self.transfer_session.id
            )
        self.assertFalse(
            Buffer.objects.filter(transfer_session_id=self.transfer_session.id).exists()
        )

    def test_dequeue_into_store(self):
        _dequeue_into_store(self.transfer_session)
        # ensure a record with different transfer session id is not affected
        self.assertTrue(
            Buffer.objects.filter(transfer_session_id=self.data["tfs_id"]).exists()
        )
        self.assertFalse(Store.objects.filter(id=self.data["model6"]).exists())
        self.assertFalse(
            RecordMaxCounter.objects.filter(
                store_model_id=self.data["model6"],
                instance_id__in=self.data["model6_rmcb_ids"],
            ).exists()
        )

        # ensure reverse fast forward records are not modified
        self.assertNotEqual(
            Store.objects.get(id=self.data["model1"]).serialized, "buffer"
        )
        self.assertFalse(
            RecordMaxCounter.objects.filter(
                instance_id=self.data["model1_rmcb_ids"][1]
            ).exists()
        )

        # ensure records with merge conflicts are modified
        self.assertEqual(
            Store.objects.get(id=self.data["model2"]).conflicting_serialized_data,
            "buffer\nstore",
        )  # conflicting field is overwritten
        self.assertEqual(
            Store.objects.get(id=self.data["model5"]).conflicting_serialized_data,
            "buffer\nstore",
        )
        self.assertTrue(
            RecordMaxCounter.objects.filter(
                instance_id=self.data["model2_rmcb_ids"][1]
            ).exists()
        )
        self.assertTrue(
            RecordMaxCounter.objects.filter(
                instance_id=self.data["model5_rmcb_ids"][1]
            ).exists()
        )
        self.assertEqual(
            Store.objects.get(id=self.data["model2"]).last_saved_instance,
            InstanceIDModel.get_or_create_current_instance()[0].id,
        )
        self.assertEqual(
            Store.objects.get(id=self.data["model5"]).last_saved_instance,
            InstanceIDModel.get_or_create_current_instance()[0].id,
        )

        # ensure fast forward records are modified
        self.assertEqual(
            Store.objects.get(id=self.data["model3"]).serialized, "buffer"
        )  # serialized field is overwritten
        self.assertTrue(
            RecordMaxCounter.objects.filter(
                instance_id=self.data["model3_rmcb_ids"][1]
            ).exists()
        )
        self.assertEqual(
            Store.objects.get(id=self.data["model3"]).last_saved_instance,
            self.data["model3_rmcb_ids"][1],
        )  # last_saved_by is updated
        self.assertEqual(
            RecordMaxCounter.objects.get(
                instance_id=self.data["model3_rmcb_ids"][0],
                store_model_id=self.data["model3"],
            ).counter,
            3,
        )

        # ensure all buffer and rmcb records were deleted for this transfer session id
        self.assertFalse(
            Buffer.objects.filter(transfer_session_id=self.transfer_session.id).exists()
        )
        self.assertFalse(
            RecordMaxCounterBuffer.objects.filter(
                transfer_session_id=self.transfer_session.id
            ).exists()
        )

    @mock.patch("morango.sync.operations.DatabaseMaxCounter.update_fsics")
    def test_local_dequeue_operation(self, mock_update_fsics):
        self.transfer_session.records_transferred = 1
        self.context.filter = [self.transfer_session.filter]
        operation = LocalDequeueOperation()
        self.assertEqual(transfer_status.COMPLETED, operation.handle(self.context))
        mock_update_fsics.assert_called()
        self.assertFalse(
            Buffer.objects.filter(transfer_session_id=self.transfer_session.id).exists()
        )

    @mock.patch("morango.sync.operations._dequeue_into_store")
    def test_local_dequeue_operation__noop(self, mock_dequeue):
        self.context.is_server = False
        operation = LocalDequeueOperation()
        self.assertEqual(transfer_status.COMPLETED, operation.handle(self.context))
        mock_dequeue.assert_not_called()

    @mock.patch("morango.sync.operations._dequeue_into_store")
    def test_local_dequeue_operation__noop__nothing_transferred(self, mock_dequeue):
        self.transfer_session.records_transferred = 0
        operation = LocalDequeueOperation()
        self.assertEqual(transfer_status.COMPLETED, operation.handle(self.context))
        mock_dequeue.assert_not_called()


class SyncSignalTestCase(TestCase):
    def test_defaults(self):
        signaler = SyncSignal(this_is_a_default=True)
        handler = mock.Mock()
        signaler.connect(handler)
        signaler.fire()

        handler.assert_called_once_with(this_is_a_default=True)

    def test_fire_with_kwargs(self):
        signaler = SyncSignal(my_key="abc")
        handler = mock.Mock()
        signaler.connect(handler)
        signaler.fire(my_key="123", not_default=True)

        handler.assert_called_once_with(my_key="123", not_default=True)


class SyncSignalGroupTestCase(TestCase):
    def test_started_defaults(self):
        signaler = SyncSignalGroup(this_is_a_default=True)
        handler = mock.Mock()
        signaler.connect(handler)

        signaler.fire()
        handler.assert_called_with(this_is_a_default=True)

        signaler.started.fire(this_is_a_default=False)
        handler.assert_called_with(this_is_a_default=False)

    def test_in_progress_defaults(self):
        signaler = SyncSignalGroup(this_is_a_default=True)
        handler = mock.Mock()
        signaler.connect(handler)

        signaler.fire()
        handler.assert_called_with(this_is_a_default=True)

        signaler.in_progress.fire(this_is_a_default=False)
        handler.assert_called_with(this_is_a_default=False)

    def test_completed_defaults(self):
        signaler = SyncSignalGroup(this_is_a_default=True)
        handler = mock.Mock()
        signaler.connect(handler)

        signaler.fire()
        handler.assert_called_with(this_is_a_default=True)

        signaler.completed.fire(this_is_a_default=False)
        handler.assert_called_with(this_is_a_default=False)

    def test_send(self):
        signaler = SyncSignalGroup(this_is_a_default=True)

        start_handler = mock.Mock()
        signaler.started.connect(start_handler)
        in_progress_handler = mock.Mock()
        signaler.in_progress.connect(in_progress_handler)
        completed_handler = mock.Mock()
        signaler.completed.connect(completed_handler)

        with signaler.send(other="A") as status:
            start_handler.assert_called_once_with(this_is_a_default=True, other="A")
            status.in_progress.fire(this_is_a_default=False, other="B")
            in_progress_handler.assert_called_once_with(
                this_is_a_default=False, other="B"
            )
            completed_handler.assert_not_called()

        completed_handler.assert_called_once_with(this_is_a_default=True, other="A")


class SyncClientSignalsTestCase(TestCase):
    def test_separation(self):
        handler1 = mock.Mock()
        signals1 = SyncClientSignals()
        signals1.session.connect(handler1)

        handler2 = mock.Mock()
        signals2 = SyncClientSignals()
        signals2.session.connect(handler2)

        signals1.session.fire()
        handler1.assert_called_once_with(transfer_session=None)
        handler2.assert_not_called()
