"""Public web response contracts. Legacy extension fields remain serialized."""
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field
from src.schemas import TaskSpec

class OpenResponse(BaseModel):
    model_config = ConfigDict(extra='allow')

class TelemetryResponse(OpenResponse):
    elapsed_seconds: float | None = None
    execution_seconds: float | None = None
    queue_seconds: float | None = None
    total_tokens: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    usage_status: str = 'unavailable'
    model_calls: int = 0
    agents: list[str] = Field(default_factory=list)

class MessageResponse(OpenResponse):
    message_id: str
    role: str
    content: str
    task_id: str | None = None
    created_at: str | None = None

class ArtifactResponse(OpenResponse):
    artifact_id: str
    task_id: str
    session_id: str
    name: str
    kind: str
    mime_type: str
    size: int
    hash: str
    downloadable: bool = True

class ReportVersionResponse(OpenResponse):
    task_id: str
    title: str = ''
    number: int | None = None
    version: str | None = None
    revision_of: dict[str, Any] | None = None
    artifacts: list[ArtifactResponse] = Field(default_factory=list)

class QuestionLogResponse(OpenResponse):
    task_id: str
    question: str
    status: str
    created_at: str | None = None
    telemetry: TelemetryResponse

class SessionResponse(OpenResponse):
    session_id: str
    title: str
    created_at: str
    messages: list[MessageResponse] = Field(default_factory=list)
    tasks: list[str] = Field(default_factory=list)
    question_logs: list[QuestionLogResponse] = Field(default_factory=list)
    report_versions: list[ReportVersionResponse] = Field(default_factory=list)

class SessionListResponse(OpenResponse):
    items: list[SessionResponse]

class TaskResultResponse(OpenResponse):
    analysis_ids: list[str] = Field(default_factory=list)
    report_ids: list[str] = Field(default_factory=list)
    final_response: str | None = None

class TaskResponse(OpenResponse):
    task_id: str
    session_id: str
    spec: TaskSpec
    status: Literal['queued','running','succeeded','failed','cancelled','interrupted','waiting_user']
    stage: str = ''
    stages: list[str] = Field(default_factory=list)
    estimate_seconds: int | None = None
    created_at: str
    updated_at: str | None = None
    final_response: str | None = None
    error: str | dict[str, Any] | None = None
    result: TaskResultResponse | None = None
    telemetry: TelemetryResponse | None = None

class AcceptedTaskResponse(OpenResponse):
    task_id: str
    status: str
    session_id: str | None = None
    message_id: str | None = None

class ArtifactListResponse(OpenResponse):
    items: list[ArtifactResponse]

class FileResponseModel(OpenResponse):
    file_id: str
    name: str
    size: int
    source: str
    session_id: str | None = None
    relative_path: str = ''
    profile_status: str | None = None
    profile_error: str | dict[str, Any] | None = None
    profile_task_id: str | None = None
    profile: dict[str, Any] | None = None
    profiles: list[dict[str, Any]] = Field(default_factory=list)

class FileListResponse(OpenResponse):
    items: list[FileResponseModel]

class EventPageResponse(OpenResponse):
    items: list[dict[str, Any]]
    cursor: int

class QueryColumnResponse(OpenResponse):
    name: str
    data_type: str | None = None

class DatabasePreviewResponse(OpenResponse):
    columns: list[QueryColumnResponse]
    preview: list[dict[str, Any]]
    row_count: int
