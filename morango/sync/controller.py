import logging
from time import sleep

from morango.constants import transfer_stage
from morango.constants import transfer_status
from morango.models.core import SyncSession
from morango.models.core import TransferSession
from morango.registry import session_middleware
from morango.utils import parse_capabilities_from_server_request
from morango.utils import CAPABILITIES

from morango.sync.operations import _deserialize_from_store
from morango.sync.operations import _serialize_into_store
from morango.sync.operations import OperationLogger
from morango.sync.syncsession import NetworkSyncConnection


logger = logging.getLogger(__name__)


def _self_referential_fk(klass_model):
    """
    Return whether this model has a self ref FK, and the name for the field
    """
    for f in klass_model._meta.concrete_fields:
        if f.related_model:
            if issubclass(klass_model, f.related_model):
                return f.attname
    return None


class MorangoProfileController(object):
    def __init__(self, profile):
        assert profile, "profile needs to be defined."
        self.profile = profile

    def serialize_into_store(self, filter=None):
        """
        Takes data from app layer and serializes the models into the store.
        """
        with OperationLogger("Serializing records", "Serialization complete"):
            _serialize_into_store(self.profile, filter=filter)

    def deserialize_from_store(self, skip_erroring=False, filter=None):
        """
        Takes data from the store and integrates into the application.
        """
        with OperationLogger("Deserializing records", "Deserialization complete"):
            # we first serialize to avoid deserialization merge conflicts
            _serialize_into_store(self.profile, filter=filter)
            _deserialize_from_store(self.profile, filter=filter, skip_erroring=skip_erroring)

    def create_network_connection(self, base_url):
        return NetworkSyncConnection(base_url=base_url)

    def create_disk_connection(path):
        raise NotImplementedError("Coming soon...")


class SessionContext(object):
    """
    Class that holds the context of a transfer, for executing transfer ops through the middleware
    """
    __slots__ = (
        'sync_session', 'transfer_session', 'stage', 'stage_status', 'capabilities'
    )

    def __init__(self, sync_session=None, transfer_session=None, stage=None, stage_status=None, capabilities=None):
        """
        :param sync_session: The sync session instance
        :type sync_session: SyncSession|None
        :param transfer_session: The current transfer session that will be operated against
        :type transfer_session: TransferSession|None
        :param stage: Independent class property for the current stage of the session
        :type stage: str|None
        :type stage_status: str|None
        :param stage_status: Independent class property for the current stage status of the session
        :param capabilities: Capabilities set that is combined (union) against our own capabilities
        :type capabilities: set|None
        """
        self.sync_session = sync_session
        self.transfer_session = transfer_session
        self.stage = stage
        self.stage_status = stage_status
        self.capabilities = set(capabilities or []) & CAPABILITIES

    def update(self, transfer_session=None, stage=None, stage_status=None, capabilities=None):
        """
        Updates the context
        :type transfer_session: TransferSession|None
        :type stage: str|None
        :type stage_status: str|None
        :type capabilities: str[]|None
        """
        self.transfer_session = transfer_session or self.transfer_session
        self.stage = stage or self.stage
        self.stage_status = stage_status or self.stage_status
        self.capabilities = set(capabilities or self.capabilities) & CAPABILITIES

        # when updating, we go ahead and update the transfer session state too and ensure that
        # we also `refresh_from_db` too so context has the up-to-date instance
        if self.transfer_session:
            self.transfer_session.refresh_from_db()
            self.transfer_session.update_state(stage=self.stage, stage_status=self.stage_status)

    @property
    def filter(self):
        return self.transfer_session.filter if self.transfer_session else None

    def __getstate__(self):
        """Return dict of simplified data for serialization"""
        return dict(
            sync_session_id=self.sync_session.id if self.sync_session else None,
            transfer_session_id=self.transfer_session.id if self.transfer_session else None,
            stage=self.stage,
            stage_status=self.stage_status,
            capabilities=self.capabilities,
        )

    def __setstate__(self, state):
        """Re-apply dict state after serialization"""
        sync_session_id = state.get("sync_session_id", None)
        if sync_session_id is not None:
            self.sync_session = SyncSession.objects.get(pk=sync_session_id)

        transfer_session_id = state.pop("transfer_session_id", None)
        if transfer_session_id is not None:
            self.transfer_session = TransferSession.objects.get(pk=transfer_session_id)
            if self.sync_session is None:
                self.sync_session = self.transfer_session.sync_session

        self.stage = state.get("stage", None)
        self.stage_status = state.get("stage_status", None)
        self.capabilities = state.get("capabilities", None)


class LocalSessionContext(SessionContext):
    """
    Class that holds the context for operating on a transfer locally
    """
    __slots__ = (
        'is_server',
    )

    def __init__(self, request=None, **kwargs):
        """
        :param request: If acting as the server, it should pass in the request, but the request
            is not serialized into context. See `is_server` prop for determining if request was
            passed in.
        :type request: django.http.request.HttpRequest
        """
        capabilities = kwargs.pop("capabilities", [])
        if request is not None:
            capabilities = parse_capabilities_from_server_request(request)

        super(LocalSessionContext, self).__init__(
            capabilities=capabilities,
            **kwargs
        )
        self.is_server = request is not None

    def __getstate__(self):
        """Return dict of simplified data for serialization"""
        state = super(LocalSessionContext, self).__getstate__()
        state.update(is_server=self.is_server)
        return state

    def __setstate__(self, state):
        """Re-apply dict state after serialization"""
        self.is_server = state.pop("is_server", False)
        super(LocalSessionContext, self).__setstate__(state)


class NetworkSessionContext(SessionContext):
    """
    Class that holds the context for operating on a transfer remotely through network connection
    """
    __slots__ = (
        'connection',
        'transfer_session',
    )

    def __init__(self, connection, **kwargs):
        """
        :param connection: The sync client connection that allows operations to execute API calls
            against the remote Morango server instance
        :type connection: NetworkSyncConnection
        """
        self.connection = connection
        super(NetworkSessionContext, self).__init__(
            capabilities=self.connection.server_info.get("capabilities", []),
            **kwargs,
        )


class SessionController(object):
    """
    Controller class that is used to execute transfer operations, like queuing and serializing,
    but does so through the middleware registry, through which allows customization of how
    those transfer stage operations are handled
    """
    __slots__ = ('middleware', 'context', 'logging_enabled')

    def __init__(self, middleware, context, enable_logging):
        """
        :type middleware: TransferMiddlewareRegistry|list
        :type context: SessionContext|LocalSessionContext|NetworkSessionContext
        :type enable_logging: bool
        """
        self.middleware = middleware
        self.context = context
        self.logging_enabled = enable_logging

    @classmethod
    def build_local(cls, request=None, sync_session=None, transfer_session=None, enable_logging=False):
        """
        Factory method that instantiates the `SessionController` with a `LocalSessionContext`
        containing the arguments, and the global middleware registry `session_middleware`

        :type request: django.http.request.HttpRequest
        :type sync_session: SyncSession|None
        :type transfer_session: TransferSession|None
        :type enable_logging: bool
        :return: A new transfer controller
        :rtype: SessionController
        """
        context = LocalSessionContext(
            request=request,
            sync_session=sync_session,
            transfer_session=transfer_session
        )
        return SessionController(session_middleware, context, enable_logging)

    @classmethod
    def build_network(cls, connection, sync_session=None, transfer_session=None):
        """
        Factory method that instantiates the `SessionController` with a `NetworkSessionContext`
        containing the arguments, and the global middleware registry `session_middleware`

        :type connection: NetworkSyncConnection
        :type sync_session: SyncSession|None
        :type transfer_session: TransferSession|None
        :return: A new transfer controller
        :rtype: SessionController
        """
        context = NetworkSessionContext(
            connection,
            sync_session=sync_session,
            transfer_session=transfer_session,
        )
        return SessionController(session_middleware, context, False)

    def proceed_to(self, stage):
        """
        Calls middleware that operates on stages between the current stage and the `to_stage`. The
        middleware are called incrementally, but in order to proceed to the next stage, the
        middleware must return a complete status. If the middleware does not return a complete
        status, that status is returned indicating that the method call has not reached `to_stage`.
        Therefore middleware can perform operations asynchronously and this can be repeatedly called
        to move forward through the transfer stages and their operations

        When invoking the middleware, if the status result is:
            PENDING: The controller will continue to invoke the middleware again when this method
                is called repeatedly, until the status result changes
            STARTED: The controller will not invoke any middleware until the the status changes,
                and assumes the "started" operation will update the state itself
            COMPLETED: The controller will proceed to invoke the middleware for the next stage
            ERRORED: The controller will not invoke any middleware until the the status changes,
                which would require codified resolution of the error outside of the controller

        :param stage: transfer_stage.* - The transfer stage to proceed to
        :type stage: str
        :return: transfer_status.* - The status of proceeding to that stage
        :rtype: str
        """
        stage = transfer_stage.stage(stage)

        # we can't 'proceed' to a stage we've already passed
        current_stage = transfer_stage.stage(self.context.stage)
        if current_stage > stage:
            return transfer_status.COMPLETED

        # See comments above, any of these statuses mean a no-op for proceeding
        if self.context.stage_status in (transfer_status.STARTED, transfer_status.ERRORED):
            return self.context.stage_status

        result = False
        # inside "session_middleware"
        for middleware in self.middleware:
            # execute middleware, up to and including the requested stage
            if transfer_stage.stage(middleware.related_stage) <= stage:
                # if the result is not completed status, then break because that means we can't
                # proceed to the next stage (yet)
                result = self._invoke_middleware(middleware)
                if result != transfer_status.COMPLETED:
                    break

        # since the middleware must handle our request, or throw an unimplemented error, this
        # should always be a non-False status
        return result

    def proceed_to_and_wait_for(self, stage, interval=5):
        """
        Same as `proceed_to` but waits for a finished status to be returned by sleeping between
        calls to `proceed_to` if status is not complete

        :param stage: transfer_stage.* - The transfer stage to proceed to
        :type stage: str
        :param interval: The time, in seconds, between repeat calls to `.proceed_to`
        :type stage: str
        :return: transfer_status.* - The status of proceeding to that stage,
            which should be `ERRORED` or `COMPLETE`
        :rtype: str
        """
        result = self.proceed_to(stage)
        while result not in transfer_status.FINISHED_STATES:
            sleep(interval)
            result = self.proceed_to(stage)
        return result

    def _log_invocation(self, stage, result=None):
        """
        Logs messages about middleware invocation, if logging is enabled. We avoid logging when
        used through the SyncClient to avoid messing up stdout
        """
        if not self.logging_enabled:
            return

        if result is None:
            logging.info("Starting stage '{}'".format(stage))
        if result == transfer_status.COMPLETED:
            logging.info("Completed stage '{}'".format(stage))
        elif result == transfer_status.STARTED:
            logging.info("Stage is in progress '{}' = '{}'".format(stage, result))
        elif result == transfer_status.ERRORED:
            logging.info("Encountered error during stage '{}'".format(stage))

    def _invoke_middleware(self, middleware):
        """
        Invokes middleware, with logging if enabled, and handles updating the transfer session state

        :type middleware: morango.registry.SessionMiddlewareOperations
        :return: transfer_status.* - The result of invoking the middleware
        :rtype: str
        """
        stage = middleware.related_stage
        self._log_invocation(stage)

        try:
            self.context.update(stage=stage, stage_status=transfer_status.PENDING)
            result = middleware(self.context)
            self._log_invocation(stage, result)
            self.context.update(stage_status=result)
            return result
        except Exception as e:
            # always log the error itself
            logging.error(e)
            self._log_invocation(stage, result=transfer_status.ERRORED)
            self.context.update(stage_status=transfer_status.ERRORED)
            return transfer_status.ERRORED

