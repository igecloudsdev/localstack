import logging
import os
import threading

from localstack import config
from localstack.aws.connect import connect_externally_to
from localstack.aws.forwarder import AwsRequestProxy
from localstack.config import is_env_true, is_persistence_enabled
from localstack.constants import DEFAULT_AWS_ACCOUNT_ID
from localstack.services.dynamodb.packages import dynamodblocal_package
from localstack.utils.common import TMP_THREADS, ShellCommandThread, get_free_tcp_port, mkdir
from localstack.utils.functions import run_safe
from localstack.utils.net import wait_for_port_closed
from localstack.utils.objects import singleton_factory
from localstack.utils.platform import Arch, get_arch
from localstack.utils.run import FuncThread, run
from localstack.utils.serving import Server
from localstack.utils.sync import retry, synchronized

LOG = logging.getLogger(__name__)
RESTART_LOCK = threading.RLock()


def _log_listener(line, **_kwargs):
    LOG.debug(line.rstrip())


class DynamodbServer(Server):
    db_path: str | None
    heap_size: str

    delay_transient_statuses: bool
    optimize_db_before_startup: bool
    share_db: bool
    cors: str | None

    proxy: AwsRequestProxy

    def __init__(
        self,
        port: int | None = None,
        host: str = "localhost",
        db_path: str | None = None,
    ) -> None:
        """
        Creates a DynamoDB server from the local configuration.

        :param port: optional, the port to start the server on (defaults to a random port)
        :param host: localhost by default
        :param db_path: path to the persistence state files used by the DynamoDB Local process
        """

        port = port or get_free_tcp_port()
        super().__init__(port, host)

        self.db_path = (
            f"{config.dirs.data}/dynamodb" if not db_path and config.dirs.data else db_path
        )

        # With persistence and MANUAL save strategy, we start DDBLocal from a temporary folder than gets copied over
        # the "usual" data directory in the `on_before_state_save` hook (see the provider code for more details).
        if is_persistence_enabled() and config.SNAPSHOT_SAVE_STRATEGY == "MANUAL":
            self.db_path = f"{config.dirs.tmp}/dynamodb"
            LOG.debug(
                "Persistence save strategy set to MANUAL. The DB path is temporarily stored at: %s",
                self.db_path,
            )

        # the DYNAMODB_IN_MEMORY variable takes precedence and will set the DB path to None which forces inMemory=true
        if is_env_true("DYNAMODB_IN_MEMORY"):
            # note: with DYNAMODB_IN_MEMORY we do not support persistence
            self.db_path = None

        if self.db_path:
            self.db_path = os.path.abspath(self.db_path)

        self.heap_size = config.DYNAMODB_HEAP_SIZE
        self.delay_transient_statuses = is_env_true("DYNAMODB_DELAY_TRANSIENT_STATUSES")
        self.optimize_db_before_startup = is_env_true("DYNAMODB_OPTIMIZE_DB_BEFORE_STARTUP")
        self.share_db = is_env_true("DYNAMODB_SHARE_DB")
        self.cors = os.getenv("DYNAMODB_CORS", None)
        self.proxy = AwsRequestProxy(self.url)

    @staticmethod
    @singleton_factory
    def get() -> "DynamodbServer":
        return DynamodbServer(config.DYNAMODB_LOCAL_PORT)

    @synchronized(lock=RESTART_LOCK)
    def start_dynamodb(self) -> bool:
        """Start the DynamoDB server."""

        # We want this method to be idempotent.
        if self.is_running() and self.is_up():
            return True

        # For the v2 provider, the DynamodbServer has been made a singleton. Yet, the Server abstraction is modelled
        # after threading.Thread, where Start -> Stop -> Start is not allowed. This flow happens during state resets.
        # The following is a workaround that permits this flow
        self._started.clear()
        self._stopped.clear()

        # Note: when starting the server, we had a flag for wiping the assets directory before the actual start.
        # This behavior was needed in some particular cases:
        # - pod load with some assets already lying in the asset folder
        # - ...
        # The cleaning is now done via the reset endpoint
        if self.db_path:
            mkdir(self.db_path)

        started = self.start()
        self.wait_for_dynamodb()
        return started

    @synchronized(lock=RESTART_LOCK)
    def stop_dynamodb(self) -> None:
        """Stop the DynamoDB server."""
        import psutil

        if self._thread is None:
            return
        self._thread.auto_restart = False
        self.shutdown()
        self.join(timeout=10)
        try:
            wait_for_port_closed(self.port, sleep_time=0.8, retries=10)
        except Exception:
            LOG.warning(
                "DynamoDB server port %s (%s) unexpectedly still open; running processes: %s",
                self.port,
                self._thread,
                run(["ps", "aux"]),
            )

            # attempt to terminate/kill the process manually
            server_pid = self._thread.process.pid  # noqa
            LOG.info("Attempting to kill DynamoDB process %s", server_pid)
            process = psutil.Process(server_pid)
            run_safe(process.terminate)
            run_safe(process.kill)
            wait_for_port_closed(self.port, sleep_time=0.5, retries=8)

    @property
    def in_memory(self) -> bool:
        return self.db_path is None

    @property
    def jar_path(self) -> str:
        return f"{dynamodblocal_package.get_installed_dir()}/DynamoDBLocal.jar"

    @property
    def library_path(self) -> str:
        return f"{dynamodblocal_package.get_installed_dir()}/DynamoDBLocal_lib"

    def _get_java_vm_options(self) -> list[str]:
        # Workaround for JVM SIGILL crash on Apple Silicon M4
        # See https://bugs.openjdk.org/browse/JDK-8345296
        # To be removed after Java is bumped to 17.0.15+ and 21.0.7+
        return ["-XX:UseSVE=0"] if Arch.arm64 == get_arch() else []

    def _create_shell_command(self) -> list[str]:
        cmd = [
            "java",
            *self._get_java_vm_options(),
            "-Xmx%s" % self.heap_size,
            f"-javaagent:{dynamodblocal_package.get_installer().get_ddb_agent_jar_path()}",
            f"-Djava.library.path={self.library_path}",
            "-jar",
            self.jar_path,
        ]
        parameters = []

        parameters.extend(["-port", str(self.port)])
        if self.in_memory:
            parameters.append("-inMemory")
        if self.db_path:
            parameters.extend(["-dbPath", self.db_path])
        if self.delay_transient_statuses:
            parameters.extend(["-delayTransientStatuses"])
        if self.optimize_db_before_startup:
            parameters.extend(["-optimizeDbBeforeStartup"])
        if self.share_db:
            parameters.extend(["-sharedDb"])

        return cmd + parameters

    def do_start_thread(self) -> FuncThread:
        dynamodblocal_installer = dynamodblocal_package.get_installer()
        dynamodblocal_installer.install()

        cmd = self._create_shell_command()
        env_vars = {
            **dynamodblocal_installer.get_java_env_vars(),
            "DDB_LOCAL_TELEMETRY": "0",
        }

        LOG.debug("Starting DynamoDB Local: %s", cmd)
        t = ShellCommandThread(
            cmd,
            strip_color=True,
            log_listener=_log_listener,
            auto_restart=True,
            name="dynamodb-local",
            env_vars=env_vars,
        )
        TMP_THREADS.append(t)
        t.start()
        return t

    def check_dynamodb(self, expect_shutdown: bool = False) -> None:
        """Checks if DynamoDB server is up"""
        out = None

        try:
            self.wait_is_up()
            out = connect_externally_to(
                endpoint_url=self.url,
                aws_access_key_id=DEFAULT_AWS_ACCOUNT_ID,
                aws_secret_access_key=DEFAULT_AWS_ACCOUNT_ID,
            ).dynamodb.list_tables()
        except Exception:
            LOG.exception("DynamoDB health check failed")
        if expect_shutdown:
            assert out is None
        else:
            assert isinstance(out["TableNames"], list)

    def wait_for_dynamodb(self) -> None:
        retry(self.check_dynamodb, sleep=0.4, retries=10)
