import mock
import pickle
from django.test import SimpleTestCase
from django.test import TestCase

from ..helpers import create_dummy_store_data
from ..helpers import TestSessionContext
from morango.constants import capabilities
from morango.constants import transfer_stages
from morango.constants import transfer_statuses
from morango.errors import MorangoContextUpdateError
from morango.models.certificates import Filter
from morango.models.core import SyncSession
from morango.models.core import TransferSession
from morango.sync.context import SessionContext
from morango.sync.context import LocalSessionContext
from morango.sync.context import NetworkSessionContext


class SessionContextTestCase(SimpleTestCase):
    def test_init__nothing(self):
        context = TestSessionContext()
        context.update_state(
            stage=transfer_stages.TRANSFERRING, stage_status=transfer_statuses.PENDING
        )
        self.assertEqual(transfer_stages.TRANSFERRING, context.stage)
        self.assertEqual(transfer_statuses.PENDING, context.stage_status)

    def test_init__capabilities__no_match(self):
        context = TestSessionContext(capabilities=["testing"])
        self.assertNotIn("testing", context.capabilities)

    @mock.patch("morango.sync.context.CAPABILITIES", {"testing"})
    def test_init__capabilities(self):
        context = TestSessionContext(capabilities=["testing"])
        self.assertIn("testing", context.capabilities)

    def test_init__no_transfer_session(self):
        sync_session = mock.Mock(spec=SyncSession)
        sync_filter = mock.Mock(spec=Filter)

        context = TestSessionContext(sync_session=sync_session, sync_filter=sync_filter, is_push=True)
        self.assertEqual(sync_session, context.sync_session)
        self.assertEqual(sync_filter, context.filter)
        self.assertTrue(context.is_push)
        self.assertFalse(context.is_pull)

    def test_init__with_transfer_session(self):
        sync_session = mock.Mock(spec=SyncSession)
        sync_filter = Filter("before_filter")
        transfer_session = mock.Mock(
            spec=TransferSession,
            sync_session=sync_session,
            push=False,
            filter="after_filter",
            transfer_stage=transfer_stages.TRANSFERRING,
            transfer_stage_status=transfer_statuses.STARTED,
        )
        transfer_session.get_filter.return_value = Filter(transfer_session.filter)

        context = TestSessionContext(transfer_session=transfer_session, sync_filter=sync_filter)
        self.assertEqual(transfer_session, context.transfer_session)
        self.assertEqual(sync_session, context.sync_session)
        self.assertEqual("after_filter", str(context.filter))
        self.assertFalse(context.is_push)
        self.assertTrue(context.is_pull)

    def test_init__with_transfer_session__no_filter(self):
        sync_session = mock.Mock(spec=SyncSession)
        sync_filter = Filter("before_filter")
        transfer_session = mock.Mock(
            spec=TransferSession,
            sync_session=sync_session,
            push=False,
            filter=None,
            transfer_stage=transfer_stages.TRANSFERRING,
            transfer_stage_status=transfer_statuses.STARTED,
        )
        self.assertIsNone(transfer_session.filter)

        context = TestSessionContext(transfer_session=transfer_session, sync_filter=sync_filter)
        self.assertEqual(transfer_session, context.transfer_session)
        self.assertEqual(sync_session, context.sync_session)
        self.assertEqual("before_filter", str(context.filter))
        self.assertFalse(context.is_push)
        self.assertTrue(context.is_pull)

    def test_update__no_overwrite__transfer_session(self):
        sync_session = mock.Mock(spec=SyncSession)
        sync_filter = mock.Mock(spec=Filter)
        transfer_session = mock.Mock(
            spec=TransferSession,
            sync_session=sync_session,
            push=False,
            transfer_stage=transfer_stages.TRANSFERRING,
            transfer_stage_status=transfer_statuses.STARTED,
        )
        transfer_session.get_filter.return_value = sync_filter

        context = SessionContext(transfer_session=transfer_session)
        with self.assertRaises(MorangoContextUpdateError):
            context.update(transfer_session=transfer_session)

    def test_update__no_overwrite__filter(self):
        sync_filter = mock.Mock(spec=Filter)
        context = SessionContext(sync_filter=sync_filter)

        with self.assertRaises(MorangoContextUpdateError):
            context.update(sync_filter=sync_filter)

    def test_update__no_overwrite__push(self):
        context = SessionContext(is_push=True)

        with self.assertRaises(MorangoContextUpdateError):
            context.update(is_push=False)

    @mock.patch("morango.sync.context.CAPABILITIES", {"testing"})
    def test_update__basic(self):
        context = TestSessionContext()

        sync_filter = mock.Mock(spec=Filter)
        context.update(
            sync_filter=sync_filter,
            is_push=True,
            stage=transfer_stages.TRANSFERRING,
            stage_status=transfer_statuses.STARTED,
            capabilities={"testing"}
        )
        self.assertEqual(sync_filter, context.filter)
        self.assertTrue(context.is_push)
        self.assertFalse(context.is_pull)
        self.assertEqual(transfer_stages.TRANSFERRING, context.stage)
        self.assertEqual(transfer_statuses.STARTED, context.stage_status)

    @mock.patch("morango.sync.context.CAPABILITIES", {"testing"})
    def test_update__with_transfer_session(self):
        context = TestSessionContext(
            capabilities={"testing"}
        )

        sync_session = mock.Mock(spec=SyncSession)
        sync_filter = mock.Mock(spec=Filter)
        transfer_session = mock.Mock(
            spec=TransferSession,
            sync_session=sync_session,
            push=True,
        )
        transfer_session.get_filter.return_value = sync_filter
        context.update(transfer_session=transfer_session)
        self.assertEqual(sync_filter, context.filter)
        self.assertTrue(context.is_push)
        self.assertFalse(context.is_pull)


class LocalSessionContextTestCase(SimpleTestCase):
    @mock.patch("morango.sync.context.CAPABILITIES", {"testing"})
    @mock.patch("morango.sync.context.parse_capabilities_from_server_request")
    def test_init(self, mock_parse_capabilities):
        mock_parse_capabilities.return_value = {"testing"}
        request = mock.Mock(spec="django.http.request.HttpRequest")
        context = LocalSessionContext(request=request)

        self.assertEqual(request, context.request)
        self.assertTrue(context.is_server)
        self.assertIn("testing", context.capabilities)

    def test_update(self):
        transfer_session = mock.Mock(
            spec=TransferSession,
            transfer_stage=transfer_stages.TRANSFERRING,
            transfer_stage_status=transfer_statuses.STARTED,
        )
        context = LocalSessionContext()
        self.assertNotEqual(transfer_stages.TRANSFERRING, context.stage)
        self.assertNotEqual(transfer_statuses.STARTED, context.stage_status)

        context.update(transfer_session=transfer_session)
        self.assertEqual(transfer_stages.TRANSFERRING, context.stage)
        self.assertEqual(transfer_statuses.STARTED, context.stage_status)

        context.update(stage=transfer_stages.CLEANUP, stage_status=transfer_statuses.PENDING)
        transfer_session.refresh_from_db.assert_called()
        transfer_session.update_state.assert_called_with(
            stage=transfer_stages.CLEANUP, stage_status=transfer_statuses.PENDING
        )


class NetworkSessionContextTestCase(SimpleTestCase):
    @mock.patch("morango.sync.context.CAPABILITIES", {"testing"})
    def test_init(self):
        conn = mock.Mock(
            spec="morango.sync.syncsession.NetworkSyncConnection",
            server_info=mock.Mock(),
        )
        conn.server_info.get.return_value = {"testing"}
        context = NetworkSessionContext(conn)
        self.assertEqual(conn, context.connection)
        self.assertIn("testing", context.capabilities)


class ContextPicklingTestCase(TestCase):
    def test_basic(self):
        data = create_dummy_store_data()
        transfer_session = data["sc"].current_transfer_session
        transfer_session.filter = "abc123"
        transfer_session.save()

        context = TestSessionContext(transfer_session=transfer_session)
        context.update(error=NotImplementedError("This is a test"))
        pickled_context = pickle.dumps(context)
        unpickled_context = pickle.loads(pickled_context)
        self.assertIsNotNone(context.transfer_session)
        self.assertEqual(context.filter, unpickled_context.filter)
        self.assertEqual(context.is_push, unpickled_context.is_push)
        self.assertEqual(context.stage, unpickled_context.stage)
        self.assertEqual(context.stage_status, unpickled_context.stage_status)
        self.assertEqual(context.capabilities, unpickled_context.capabilities)
        self.assertIsInstance(unpickled_context.error, NotImplementedError)
        self.assertEqual(str(context.error), str(unpickled_context.error))

    @mock.patch("morango.sync.context.parse_capabilities_from_server_request")
    def test_local(self, mock_parse_capabilities):
        request = mock.Mock(spec="django.http.request.HttpRequest")
        mock_parse_capabilities.return_value = {}

        context = LocalSessionContext(request=request)
        pickled_context = pickle.dumps(context)
        unpickled_context = pickle.loads(pickled_context)
        self.assertEqual(context.is_push, unpickled_context.is_push)
        self.assertEqual(context.stage, unpickled_context.stage)
        self.assertEqual(context.stage_status, unpickled_context.stage_status)
        self.assertEqual(context.capabilities, unpickled_context.capabilities)
        self.assertEqual(context.is_server, unpickled_context.is_server)

    @mock.patch("morango.sync.context.parse_capabilities_from_server_request")
    def test_network(self, mock_parse_capabilities):
        conn = mock.Mock(spec="morango.sync.syncsession.NetworkSyncConnection",
                         server_info=dict(capabilities={}))
        mock_parse_capabilities.return_value = {}

        context = NetworkSessionContext(conn)
        context.update_state(
            stage=transfer_stages.TRANSFERRING, stage_status=transfer_statuses.PENDING
        )
        pickled_context = pickle.dumps(context)
        unpickled_context = pickle.loads(pickled_context)
        self.assertEqual(context.is_push, unpickled_context.is_push)
        self.assertEqual(context.stage, unpickled_context.stage)
        self.assertEqual(context.stage_status, unpickled_context.stage_status)
        self.assertEqual(context.capabilities, unpickled_context.capabilities)

