import collections.abc
import datetime as dt
import typing
from dataclasses import asdict, dataclass, fields
from uuid import UUID

import structlog
import temporalio
import temporalio.common
from asgiref.sync import async_to_sync
from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleIntervalSpec,
    ScheduleOverlapPolicy,
    SchedulePolicy,
    ScheduleSpec,
    ScheduleState,
)

from posthog.batch_exports.models import (
    BatchExport,
    BatchExportBackfill,
    BatchExportDestination,
    BatchExportRun,
)
from posthog.clickhouse.client import sync_execute
from posthog.constants import BATCH_EXPORTS_TASK_QUEUE, SYNC_BATCH_EXPORTS_TASK_QUEUE
from posthog.temporal.common.client import sync_connect
from posthog.temporal.common.schedule import (
    a_pause_schedule,
    create_schedule,
    delete_schedule,
    pause_schedule,
    unpause_schedule,
    update_schedule,
)

logger = structlog.get_logger(__name__)


class BatchExportField(typing.TypedDict):
    """A field to be queried from ClickHouse.

    Attributes:
        expression: A ClickHouse SQL expression that declares the field required.
        alias: An alias to apply to the expression (after an 'AS' keyword).
    """

    expression: str
    alias: str


class BatchExportSchema(typing.TypedDict):
    fields: list[BatchExportField]
    values: dict[str, str]


@dataclass
class BatchExportModel:
    name: str
    schema: BatchExportSchema | None


class BatchExportsInputsProtocol(typing.Protocol):
    team_id: int
    batch_export_model: BatchExportModel | None = None
    is_backfill: bool = False


@dataclass
class S3BatchExportInputs:
    """Inputs for S3 export workflow.

    Attributes:
        batch_export_id: The ID of the parent BatchExport.
        team_id: The ID of the team that contains the BatchExport whose data we are exporting.
        interval: The range of data we are exporting.
        bucket_name: The S3 bucket we are exporting to.
        region: The AWS region where the bucket is located.
        prefix: A prefix for the file name to be created in S3.
            For example, for one hour batches, this should be 3600.
        max_file_size_mb: The maximum file size in MB for each file to be uploaded.
        data_interval_end: For manual runs, the end date of the batch. This should be set to `None` for regularly
            scheduled runs and for backfills.
    """

    batch_export_id: str
    team_id: int
    bucket_name: str
    region: str
    prefix: str
    interval: str = "hour"
    aws_access_key_id: str | None = None
    aws_secret_access_key: str | None = None
    data_interval_end: str | None = None
    compression: str | None = None
    exclude_events: list[str] | None = None
    include_events: list[str] | None = None
    encryption: str | None = None
    kms_key_id: str | None = None
    endpoint_url: str | None = None
    file_format: str = "JSONLines"
    max_file_size_mb: int | None = None
    is_backfill: bool = False
    is_earliest_backfill: bool = False
    batch_export_model: BatchExportModel | None = None
    batch_export_schema: BatchExportSchema | None = None

    def __post_init__(self):
        if self.max_file_size_mb:
            self.max_file_size_mb = int(self.max_file_size_mb)


@dataclass
class SnowflakeBatchExportInputs:
    """Inputs for Snowflake export workflow."""

    batch_export_id: str
    team_id: int
    user: str
    password: str
    account: str
    database: str
    warehouse: str
    schema: str
    interval: str = "hour"
    table_name: str = "events"
    data_interval_end: str | None = None
    role: str | None = None
    exclude_events: list[str] | None = None
    include_events: list[str] | None = None
    is_backfill: bool = False
    is_earliest_backfill: bool = False
    batch_export_model: BatchExportModel | None = None
    batch_export_schema: BatchExportSchema | None = None


@dataclass
class PostgresBatchExportInputs:
    """Inputs for Postgres export workflow."""

    batch_export_id: str
    team_id: int
    user: str
    password: str
    host: str
    database: str
    has_self_signed_cert: bool = False
    interval: str = "hour"
    schema: str = "public"
    table_name: str = "events"
    port: int = 5432
    data_interval_end: str | None = None
    exclude_events: list[str] | None = None
    include_events: list[str] | None = None
    is_backfill: bool = False
    is_earliest_backfill: bool = False
    batch_export_model: BatchExportModel | None = None
    batch_export_schema: BatchExportSchema | None = None

    def __post_init__(self):
        if self.has_self_signed_cert == "True":  # type: ignore
            self.has_self_signed_cert = True
        elif self.has_self_signed_cert == "False":  # type: ignore
            self.has_self_signed_cert = False

        self.port = int(self.port)


@dataclass
class RedshiftBatchExportInputs(PostgresBatchExportInputs):
    """Inputs for Redshift export workflow."""

    properties_data_type: str = "varchar"


@dataclass
class BigQueryBatchExportInputs:
    """Inputs for BigQuery export workflow."""

    batch_export_id: str
    team_id: int
    project_id: str
    dataset_id: str
    private_key: str
    private_key_id: str
    token_uri: str
    client_email: str
    interval: str = "hour"
    table_id: str = "events"
    data_interval_end: str | None = None
    exclude_events: list[str] | None = None
    include_events: list[str] | None = None
    use_json_type: bool = False
    is_backfill: bool = False
    is_earliest_backfill: bool = False

    batch_export_model: BatchExportModel | None = None
    batch_export_schema: BatchExportSchema | None = None

    def __post_init__(self):
        if self.use_json_type == "True":  # type: ignore
            self.use_json_type = True
        elif self.use_json_type == "False":  # type: ignore
            self.use_json_type = False


@dataclass
class HttpBatchExportInputs:
    """Inputs for Http export workflow."""

    batch_export_id: str
    team_id: int
    url: str
    token: str
    interval: str = "hour"
    data_interval_end: str | None = None
    exclude_events: list[str] | None = None
    include_events: list[str] | None = None
    is_backfill: bool = False
    is_earliest_backfill: bool = False
    batch_export_model: BatchExportModel | None = None
    batch_export_schema: BatchExportSchema | None = None


@dataclass
class NoOpInputs:
    """NoOp Workflow is used for testing, it takes a single argument to echo back."""

    batch_export_id: str
    team_id: int
    interval: str = "hour"
    arg: str = ""
    is_backfill: bool = False
    batch_export_model: BatchExportModel | None = None
    batch_export_schema: BatchExportSchema | None = None


DESTINATION_WORKFLOWS = {
    "S3": ("s3-export", S3BatchExportInputs),
    "Snowflake": ("snowflake-export", SnowflakeBatchExportInputs),
    "Postgres": ("postgres-export", PostgresBatchExportInputs),
    "Redshift": ("redshift-export", RedshiftBatchExportInputs),
    "BigQuery": ("bigquery-export", BigQueryBatchExportInputs),
    "HTTP": ("http-export", HttpBatchExportInputs),
    "NoOp": ("no-op", NoOpInputs),
}


class BatchExportServiceError(Exception):
    """Base class for BatchExport service exceptions."""


class BatchExportIdError(BatchExportServiceError):
    """Exception raised when an id for a BatchExport is not found."""

    def __init__(self, batch_export_id: str):
        super().__init__(f"No BatchExport found with ID: '{batch_export_id}'")


class BatchExportServiceRPCError(BatchExportServiceError):
    """Exception raised when the underlying Temporal RPC fails."""


class BatchExportWithNoEndNotAllowedError(BatchExportServiceError):
    """Exception raised when a BatchExport without an end_at is not allowed for a given destination."""


class BatchExportServiceScheduleNotFound(BatchExportServiceRPCError):
    """Exception raised when the underlying Temporal RPC fails because a schedule was not found."""

    def __init__(self, schedule_id: str):
        self.schedule_id = schedule_id
        super().__init__(f"The Temporal Schedule {schedule_id} was not found (maybe it was deleted?)")


def pause_batch_export(temporal: Client, batch_export_id: str, note: str | None = None) -> bool:
    """Pause this BatchExport.

    We pass the call to the underlying Temporal Schedule.

    Returns:
        `True` if the batch export was paused, `False` if it was already paused.
    """
    try:
        batch_export = BatchExport.objects.get(id=batch_export_id)
    except BatchExport.DoesNotExist:
        raise BatchExportIdError(batch_export_id)

    if batch_export.paused is True:
        return False

    try:
        pause_schedule(temporal, schedule_id=batch_export_id, note=note)
    except Exception as exc:
        raise BatchExportServiceRPCError(f"BatchExport {batch_export_id} could not be paused") from exc

    batch_export.paused = True
    batch_export.last_paused_at = dt.datetime.now(dt.UTC)
    batch_export.save()

    return True


async def apause_batch_export(temporal: Client, batch_export_id: str, note: str | None = None) -> bool:
    """Pause this BatchExport.

    We pass the call to the underlying Temporal Schedule.

    Returns:
        `True` if the batch export was paused, `False` if it was already paused.
    """
    try:
        batch_export = await BatchExport.objects.aget(id=batch_export_id)
    except BatchExport.DoesNotExist:
        raise BatchExportIdError(batch_export_id)

    if batch_export.paused is True:
        return False

    try:
        await a_pause_schedule(temporal, schedule_id=batch_export_id, note=note)
    except Exception as exc:
        raise BatchExportServiceRPCError(f"BatchExport {batch_export_id} could not be paused") from exc

    batch_export.paused = True
    batch_export.last_paused_at = dt.datetime.now(dt.UTC)
    await batch_export.asave()

    return True


def unpause_batch_export(
    temporal: Client,
    batch_export_id: str,
    note: str | None = None,
    backfill: bool = False,
) -> None:
    """Unpause this BatchExport.

    We pass the call to the underlying Temporal Schedule. Additionally, we can trigger a backfill
    to backfill runs missed while paused.

    Args:
        temporal: The Temporal client to execute calls.
        batch_export_id: The ID of the BatchExport to unpause.
        note: An optional note to include in the Schedule when unpausing.
        backfill: If True, a backfill will be triggered since the BatchExport's last_paused_at.

    Raises:
        BatchExportIdError: If the provided batch_export_id doesn't point to an existing BatchExport.
    """
    try:
        batch_export = BatchExport.objects.get(id=batch_export_id)
    except BatchExport.DoesNotExist:
        raise BatchExportIdError(batch_export_id)

    if batch_export.paused is False:
        return

    try:
        unpause_schedule(temporal, schedule_id=batch_export_id, note=note)
    except Exception as exc:
        raise BatchExportServiceRPCError(f"BatchExport {batch_export_id} could not be unpaused") from exc

    batch_export.paused = False
    batch_export.save()

    if backfill is False:
        return

    batch_export.refresh_from_db()
    start_at = batch_export.last_paused_at
    end_at = batch_export.last_updated_at

    backfill_export(temporal, batch_export_id, batch_export.team_id, start_at, end_at)


def disable_and_delete_export(instance: BatchExport):
    """Mark a BatchExport as deleted and delete its Temporal Schedule (including backfills)."""
    temporal = sync_connect()

    instance.deleted = True

    for backfill in running_backfills_for_batch_export(instance.id):
        try:
            async_to_sync(cancel_running_batch_export_backfill)(temporal, backfill)
        except Exception:
            logger.exception(
                "Failed to delete backfill %s for batch export %s, but will continue on with delete",
                backfill.id,
                instance.id,
            )

    try:
        batch_export_delete_schedule(temporal, str(instance.pk))
    except BatchExportServiceScheduleNotFound as e:
        logger.warning(
            "The Schedule %s could not be deleted as it was not found",
            e.schedule_id,
        )

    instance.save()


def batch_export_delete_schedule(temporal: Client, schedule_id: str) -> None:
    """Delete a Temporal Schedule."""
    try:
        delete_schedule(temporal, schedule_id)
    except temporalio.service.RPCError as e:
        if e.status == temporalio.service.RPCStatusCode.NOT_FOUND:
            raise BatchExportServiceScheduleNotFound(schedule_id)
        else:
            raise BatchExportServiceRPCError() from e


def running_backfills_for_batch_export(batch_export_id: UUID):
    """Return an iterator over running batch export backfills."""
    return BatchExportBackfill.objects.filter(
        batch_export_id=batch_export_id, status=BatchExportBackfill.Status.RUNNING
    ).select_related("batch_export")


async def cancel_running_batch_export_backfill(temporal: Client, batch_export_backfill: BatchExportBackfill) -> None:
    """Delete a running BatchExportBackfill.

    A BatchExportBackfill represents a Temporal Workflow. When deleting the Temporal
    Schedule that we are backfilling, we should also clean-up any Workflows that are
    still running.
    """
    handle = temporal.get_workflow_handle(workflow_id=batch_export_backfill.workflow_id)
    await handle.cancel()

    batch_export_backfill.status = BatchExportBackfill.Status.CANCELLED
    await batch_export_backfill.asave()


def cancel_running_batch_export_run(temporal: Client, batch_export_run: BatchExportRun) -> None:
    """Cancel a running BatchExportRun."""

    handle = temporal.get_workflow_handle(workflow_id=batch_export_run.workflow_id)
    async_to_sync(handle.cancel)()

    batch_export_run.status = BatchExportRun.Status.CANCELLED
    batch_export_run.save()


@dataclass
class BackfillBatchExportInputs:
    """Inputs for the BackfillBatchExport Workflow."""

    team_id: int
    batch_export_id: str
    start_at: str | None
    end_at: str | None
    buffer_limit: int = 1
    start_delay: float = 1.0


def backfill_export(
    temporal: Client,
    batch_export_id: str,
    team_id: int,
    start_at: dt.datetime | None,
    end_at: dt.datetime | None,
) -> str:
    """Starts a backfill for given team and batch export covering given date range.

    Arguments:
        temporal: A Temporal Client to trigger the workflow.
        batch_export_id: The id of the BatchExport to backfill.
        team_id: The id of the Team the BatchExport belongs to.
        start_at: From when to backfill.
        end_at: Up to when to backfill, if None it will backfill until it has caught up with realtime
                and then unpause the underlying BatchExport.
    """
    try:
        batch_export = BatchExport.objects.select_related("destination").get(id=batch_export_id, team_id=team_id)
    except BatchExport.DoesNotExist:
        raise BatchExportIdError(batch_export_id)

    # Ensure we don't allow users access to this feature until we are ready.
    if not end_at and batch_export.destination.type not in (
        BatchExportDestination.Destination.HTTP,
        BatchExportDestination.Destination.NOOP,  # For tests.
    ):
        raise BatchExportWithNoEndNotAllowedError(f"BatchExport {batch_export_id} has no end_at and is not HTTP")

    inputs = BackfillBatchExportInputs(
        batch_export_id=batch_export_id,
        team_id=team_id,
        start_at=start_at.isoformat() if start_at else None,
        end_at=end_at.isoformat() if end_at else None,
    )
    start_at_utc_str = start_at.astimezone(tz=dt.UTC).isoformat() if start_at else "START"
    # TODO: Should we use another signal besides "None"? i.e. "Inf" or "END".
    # Keeping it like this for now for backwards compatibility.
    end_at_utc_str = end_at.astimezone(tz=dt.UTC).isoformat() if end_at else "END"

    workflow_id = f"{inputs.batch_export_id}-Backfill-{start_at_utc_str}-{end_at_utc_str}"

    workflow_id = start_backfill_batch_export_workflow(temporal, workflow_id, inputs=inputs)
    return workflow_id


@async_to_sync
async def start_backfill_batch_export_workflow(
    temporal: Client, workflow_id: str, inputs: BackfillBatchExportInputs
) -> str:
    """Async call to start a BackfillBatchExportWorkflow."""
    await temporal.start_workflow(
        "backfill-batch-export",
        inputs,
        id=workflow_id,
        task_queue=BATCH_EXPORTS_TASK_QUEUE,
    )

    return workflow_id


def create_batch_export_run(
    batch_export_id: UUID,
    data_interval_start: str | None,
    data_interval_end: str,
    status: str = BatchExportRun.Status.STARTING,
    records_total_count: int | None = None,
) -> BatchExportRun:
    """Create a BatchExportRun after a Temporal Workflow execution.

    In a first approach, this method is intended to be called only by Temporal Workflows,
    as only the Workflows themselves can know when they start.

    Args:
        batch_export_id: The UUID of the BatchExport the BatchExportRun to create belongs to.
        data_interval_start: The start of the period of data exported in this BatchExportRun.
        data_interval_end: The end of the period of data exported in this BatchExportRun.
        status: The initial status for the created BatchExportRun.
    """
    run = BatchExportRun(
        batch_export_id=batch_export_id,
        status=status,
        data_interval_start=dt.datetime.fromisoformat(data_interval_start) if data_interval_start else None,
        data_interval_end=dt.datetime.fromisoformat(data_interval_end),
        records_total_count=records_total_count,
    )
    run.save()

    return run


async def acreate_batch_export_run(
    batch_export_id: UUID,
    data_interval_start: str,
    data_interval_end: str,
    status: str = BatchExportRun.Status.STARTING,
    records_total_count: int | None = None,
) -> BatchExportRun:
    """Create a BatchExportRun after a Temporal Workflow execution.

    In a first approach, this method is intended to be called only by Temporal Workflows,
    as only the Workflows themselves can know when they start.

    Args:
        batch_export_id: The UUID of the BatchExport the BatchExportRun to create belongs to.
        data_interval_start: The start of the period of data exported in this BatchExportRun.
        data_interval_end: The end of the period of data exported in this BatchExportRun.
        status: The initial status for the created BatchExportRun.
    """
    run = BatchExportRun(
        batch_export_id=batch_export_id,
        status=status,
        data_interval_start=dt.datetime.fromisoformat(data_interval_start),
        data_interval_end=dt.datetime.fromisoformat(data_interval_end),
        records_total_count=records_total_count,
    )
    await run.asave()

    return run


def update_batch_export_run(
    run_id: UUID,
    **kwargs,
) -> BatchExportRun:
    """Update the BatchExportRun with given run_id and provided **kwargs.

    Arguments:
        run_id: The id of the BatchExportRun to update.
    """
    model = BatchExportRun.objects.filter(id=run_id)
    update_at = dt.datetime.now(dt.UTC)

    updated = model.update(
        **kwargs,
        last_updated_at=update_at,
    )

    if not updated:
        raise ValueError(f"BatchExportRun with id {run_id} not found.")

    return model.get()


async def aupdate_batch_export_run(
    run_id: UUID,
    **kwargs,
) -> BatchExportRun:
    """Update the BatchExportRun with given run_id and provided **kwargs.

    Arguments:
        run_id: The id of the BatchExportRun to update.
    """
    model = BatchExportRun.objects.filter(id=run_id)
    update_at = dt.datetime.now(dt.UTC)

    updated = await model.aupdate(
        **kwargs,
        last_updated_at=update_at,
    )

    if not updated:
        raise ValueError(f"BatchExportRun with id {run_id} not found.")

    return await model.aget()


def count_failed_batch_export_runs(batch_export_id: UUID, last_n: int) -> int:
    """Count failed batch export runs in the 'last_n' runs."""
    count_of_failures = (
        BatchExportRun.objects.filter(
            id__in=BatchExportRun.objects.filter(batch_export_id=batch_export_id)
            .order_by("-last_updated_at")
            .values("id")[:last_n]
        )
        .filter(status=BatchExportRun.Status.FAILED)
        .count()
    )

    return count_of_failures


async def acount_failed_batch_export_runs(batch_export_id: UUID, last_n: int) -> int:
    """Count failed batch export runs in the 'last_n' runs."""
    count_of_failures = (
        await BatchExportRun.objects.filter(
            id__in=BatchExportRun.objects.filter(batch_export_id=batch_export_id)
            .order_by("-last_updated_at")
            .values("id")[:last_n]
        )
        .filter(status=BatchExportRun.Status.FAILED)
        .acount()
    )

    return count_of_failures


def sync_batch_export(batch_export: BatchExport, created: bool):
    workflow, workflow_inputs = DESTINATION_WORKFLOWS[batch_export.destination.type]
    state = ScheduleState(
        note=f"Schedule updated for BatchExport {batch_export.id} to Destination {batch_export.destination.id} in Team {batch_export.team.id}.",
        paused=batch_export.paused,
    )

    destination_config_fields = {field.name for field in fields(workflow_inputs)}
    destination_config = {k: v for k, v in batch_export.destination.config.items() if k in destination_config_fields}
    task_queue = SYNC_BATCH_EXPORTS_TASK_QUEUE if batch_export.destination.type == "HTTP" else BATCH_EXPORTS_TASK_QUEUE

    temporal = sync_connect()
    schedule = Schedule(
        action=ScheduleActionStartWorkflow(
            workflow,
            asdict(
                workflow_inputs(
                    team_id=batch_export.team.id,
                    batch_export_id=str(batch_export.id),
                    interval=str(batch_export.interval),
                    batch_export_model=BatchExportModel(
                        name=batch_export.model or "events",
                        schema=batch_export.schema,
                    ),
                    # TODO: This field is deprecated, but we still set it for backwards compatibility.
                    # New exports created will always have `batch_export_schema` set to `None`, but existing
                    # batch exports may still be using it.
                    # This assignment should be removed after updating all existing exports to use
                    # `batch_export_model` instead.
                    batch_export_schema=None,
                    **destination_config,
                )
            ),
            id=str(batch_export.id),
            task_queue=task_queue,
            retry_policy=temporalio.common.RetryPolicy(
                initial_interval=dt.timedelta(seconds=10),
                maximum_interval=dt.timedelta(seconds=60),
                maximum_attempts=2,
                non_retryable_error_types=["ActivityError", "ApplicationError", "CancelledError"],
            ),
        ),
        spec=ScheduleSpec(
            start_at=batch_export.start_at,
            end_at=batch_export.end_at,
            intervals=[ScheduleIntervalSpec(every=batch_export.interval_time_delta)],
            jitter=max(dt.timedelta(minutes=1), (batch_export.interval_time_delta / 6)),
            time_zone_name=batch_export.team.timezone,
        ),
        state=state,
        policy=SchedulePolicy(overlap=ScheduleOverlapPolicy.ALLOW_ALL),
    )

    if created:
        create_schedule(temporal, id=str(batch_export.id), schedule=schedule)
    else:
        # For the time being, do not update existing time_zone_name to avoid losing
        # data due to the shift in start times.
        # TODO: This should require input from the user for example when changing a project's timezone.
        # With user's input, then we can more confidently do the update.
        update_schedule(temporal, id=str(batch_export.id), schedule=schedule, keep_tz=True)

    return batch_export


def create_batch_export_backfill(
    batch_export_id: UUID,
    team_id: int,
    start_at: str | None,
    end_at: str | None,
    status: str = BatchExportRun.Status.RUNNING,
) -> BatchExportBackfill:
    """Create a BatchExportBackfill.


    Args:
        batch_export_id: The UUID of the BatchExport the BatchExportBackfill to create belongs to.
        team_id: The id of the Team the BatchExportBackfill to create belongs to.
        start_at: The start of the period to backfill in this BatchExportBackfill.
        end_at: The end of the period to backfill in this BatchExportBackfill.
        status: The initial status for the created BatchExportBackfill.
    """
    backfill = BatchExportBackfill(
        batch_export_id=batch_export_id,
        status=status,
        start_at=dt.datetime.fromisoformat(start_at) if start_at else None,
        end_at=dt.datetime.fromisoformat(end_at) if end_at else None,
        team_id=team_id,
    )
    backfill.save()

    return backfill


async def acreate_batch_export_backfill(
    batch_export_id: UUID,
    team_id: int,
    start_at: str,
    end_at: str | None,
    status: str = BatchExportRun.Status.RUNNING,
) -> BatchExportBackfill:
    """Create a BatchExportBackfill.


    Args:
        batch_export_id: The UUID of the BatchExport the BatchExportBackfill to create belongs to.
        team_id: The id of the Team the BatchExportBackfill to create belongs to.
        start_at: The start of the period to backfill in this BatchExportBackfill.
        end_at: The end of the period to backfill in this BatchExportBackfill.
        status: The initial status for the created BatchExportBackfill.
    """
    backfill = BatchExportBackfill(
        batch_export_id=batch_export_id,
        status=status,
        start_at=dt.datetime.fromisoformat(start_at),
        end_at=dt.datetime.fromisoformat(end_at) if end_at else None,
        team_id=team_id,
    )
    await backfill.asave()

    return backfill


def update_batch_export_backfill_status(backfill_id: UUID, status: str) -> BatchExportBackfill:
    """Update the status of an BatchExportBackfill with given id.

    Arguments:
        id: The id of the BatchExportBackfill to update.
        status: The new status to assign to the BatchExportBackfill.
    """
    model = BatchExportBackfill.objects.filter(id=backfill_id)
    updated = model.update(status=status)

    if not updated:
        raise ValueError(f"BatchExportBackfill with id {backfill_id} not found.")

    return model.get()


async def aupdate_batch_export_backfill_status(backfill_id: UUID, status: str) -> BatchExportBackfill:
    """Update the status of an BatchExportBackfill with given id.

    Arguments:
        id: The id of the BatchExportBackfill to update.
        status: The new status to assign to the BatchExportBackfill.
    """
    model = BatchExportBackfill.objects.filter(id=backfill_id)
    updated = await model.aupdate(status=status)

    if not updated:
        raise ValueError(f"BatchExportBackfill with id {backfill_id} not found.")

    return await model.aget()


async def aupdate_records_total_count(
    batch_export_id: UUID, interval_start: dt.datetime, interval_end: dt.datetime, count: int
) -> int:
    """Update the expected records count for a set of batch export runs.

    Typically, there is one batch export run per batch export interval, however
    there could be multiple if data has been backfilled.
    """
    rows_updated = await BatchExportRun.objects.filter(
        batch_export_id=batch_export_id,
        data_interval_start=interval_start,
        data_interval_end=interval_end,
    ).aupdate(records_total_count=count)
    return rows_updated


async def afetch_batch_export_runs_in_range(
    batch_export_id: UUID,
    interval_start: dt.datetime,
    interval_end: dt.datetime,
) -> list[BatchExportRun]:
    """Async fetch all BatchExportRuns for a given batch export within a time interval.

    Arguments:
        batch_export_id: The UUID of the BatchExport to fetch runs for.
        interval_start: The start of the time interval to fetch runs from.
        interval_end: The end of the time interval to fetch runs until.

    Returns:
        A list of BatchExportRun objects within the given interval, ordered by data_interval_start.
    """
    queryset = BatchExportRun.objects.filter(
        batch_export_id=batch_export_id,
        data_interval_start__gte=interval_start,
        data_interval_end__lte=interval_end,
    ).order_by("data_interval_start")

    return [run async for run in queryset]


def fetch_earliest_backfill_start_at(
    *,
    team_id: int,
    model: str,
    interval_time_delta: dt.timedelta,
    exclude_events: collections.abc.Iterable[str] | None = None,
    include_events: collections.abc.Iterable[str] | None = None,
) -> dt.datetime | None:
    """Get the earliest start_at for a batch export backfill.

    If there is no data for the given model, return None.
    """
    interval_seconds = int(interval_time_delta.total_seconds())
    if model == "events":
        exclude_events = exclude_events or []
        include_events = include_events or []
        query = """
            SELECT toStartOfInterval(MIN(timestamp), INTERVAL %(interval_seconds)s SECONDS)
            FROM events
            WHERE team_id = %(team_id)s
            AND (length(%(include_events)s::Array(String)) = 0 OR event IN %(include_events)s::Array(String))
            AND (length(%(exclude_events)s::Array(String)) = 0 OR event NOT IN %(exclude_events)s::Array(String))
        """
        query_args = {
            "team_id": team_id,
            "include_events": include_events,
            "exclude_events": exclude_events,
            "interval_seconds": interval_seconds,
        }
        result = sync_execute(query, query_args)[0][0]
        # if no data, ClickHouse returns 1970-01-01 00:00:00
        # (we just compare the year rather than the whole object because in some cases the timestamp returned by
        # ClickHouse has a timezone and sometimes it doesn't)
        if result.year == 1970:
            return None
        return result
    elif model == "persons":
        # In the case of persons, we need to check 2 tables: person and person_distinct_id2
        # It's more efficient querying both tables separately and taking the minimum timestamp, rather than trying to
        # join them together.
        # In some cases we might have invalid timestamps, so we use an arbitrary date in the past to filter these out.
        query = """
            SELECT toStartOfInterval(MIN(_timestamp), INTERVAL %(interval_seconds)s SECONDS)
            FROM person
            WHERE team_id = %(team_id)s
            AND _timestamp > '2000-01-01'
            UNION ALL
            SELECT toStartOfInterval(MIN(_timestamp), INTERVAL %(interval_seconds)s SECONDS)
            FROM person_distinct_id2
            WHERE team_id = %(team_id)s
            AND _timestamp > '2000-01-01'
        """
        query_args = {
            "team_id": team_id,
            "interval_seconds": interval_seconds,
        }
        results = sync_execute(query, query_args)
        # if no data, ClickHouse returns 1970-01-01 00:00:00
        # (we just compare the year rather than the whole object because in some cases the timestamp returned by
        # ClickHouse has a timezone and sometimes it doesn't)
        results = [result[0] for result in results if result[0].year != 1970]
        if not results:
            return None
        return min(results)
    else:
        raise ValueError(f"Invalid model: {model}")
