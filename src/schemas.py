"""Versioned application contracts shared by API, worker and agent tools."""
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4
from pydantic import BaseModel, ConfigDict, Field

def new_id() -> str:
    return uuid4().hex

def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()

class Record(BaseModel):
    model_config = ConfigDict(extra='forbid')
    schema_version: str = '1.0'

class MessageRequest(Record):
    content: str = Field(min_length=1, max_length=20000)
    file_ids: list[str] = Field(default_factory=list, max_length=10)
    expected_outputs: list[str] = Field(default_factory=list)
    idempotency_key: str | None = Field(default=None, max_length=128)
    analysis_ids: list[str] = Field(default_factory=list)
    reply_to_task_id: str | None = None
    base_report_id: str | None = Field(default=None, min_length=1, max_length=80)
    database_source_ids: list[str] = Field(default_factory=list, max_length=10)
    database_tables: dict[str, list[str]] = Field(default_factory=dict)

class TaskSpec(Record):
    session_id: str
    task_id: str = Field(default_factory=new_id)
    parent_task_id: str | None = None
    intent: str = 'auto'
    request: str
    file_ids: list[str] = Field(default_factory=list)
    filters: dict[str, Any] = Field(default_factory=dict)
    expected_outputs: list[str] = Field(default_factory=list)
    constraints: dict[str, Any] = Field(default_factory=dict)
    analysis_ids: list[str] = Field(default_factory=list)
    database_source_ids: list[str] = Field(default_factory=list)
    database_tables: dict[str, list[str]] = Field(default_factory=dict)

class DatasetProfile(Record):
    dataset_id: str
    file_hash: str
    sheet_or_table: str | None = None
    columns: list[str] = Field(default_factory=list)
    dtypes: dict[str, str] = Field(default_factory=dict)
    row_count: int = 0
    quality_warnings: list[str] = Field(default_factory=list)
    preview: list[dict[str, Any]] = Field(default_factory=list)

class Artifact(Record):
    artifact_id: str = Field(default_factory=new_id)
    task_id: str
    session_id: str = ''
    kind: str
    relative_path: str
    mime_type: str
    hash: str
    size: int = Field(ge=0)
    source_ids: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=utcnow)
    name: str = ''

class AnalysisResult(Record):
    status: Literal['completed', 'failed', 'needs_clarification'] = 'completed'
    analysis_id: str = Field(default_factory=new_id)
    task_id: str = ''
    session_id: str = ''
    input_versions: dict[str, str] = Field(default_factory=dict)
    metric_definitions: dict[str, Any] = Field(default_factory=dict)
    metrics: dict[str, Any] = Field(default_factory=dict)
    table_ids: list[str] = Field(default_factory=list)
    chart_ids: list[str] = Field(default_factory=list)
    findings: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    script_ids: list[str] = Field(default_factory=list)
    kb_refs: list[dict[str, Any]] = Field(default_factory=list)
    db_refs: list[dict[str, str]] = Field(default_factory=list)

class ReportRequest(Record):
    format: list[Literal['html', 'xlsx']] = Field(default_factory=lambda: ['html', 'xlsx'])
    audience: str = 'general'
    analysis_ids: list[str] = Field(default_factory=list)
    source_material_ids: list[str] = Field(default_factory=list)
    sections: list[str] = Field(default_factory=list)
    style: str = 'professional'
    output_language: str = 'zh-CN'

class ReportMetricReference(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    analysis_id: str = Field(min_length=1)
    metric_key: str = Field(min_length=1)
    label: str = ''

class ReportSection(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    heading: str = '分析结果'
    text: str = ''
    metric_refs: list[ReportMetricReference] = Field(default_factory=list)
    table_ids: list[str] = Field(default_factory=list)
    chart_ids: list[str] = Field(default_factory=list)

class ReportNarrative(BaseModel):
    """Model-authored prose and existing source references; never numeric data."""
    model_config = ConfigDict(extra='forbid', strict=True)
    summary: str = ''
    sections: list[ReportSection] = Field(default_factory=list, max_length=100)
    limitations: list[str] = Field(default_factory=list)


class ReportTextReplacement(BaseModel):
    """Replace existing report text; numbers stay bound to the original text."""
    model_config = ConfigDict(extra='forbid', strict=True)
    field: Literal['title', 'summary', 'findings', 'limitations', 'quality', 'section_heading', 'section_text', 'metric_label']
    original: str = Field(min_length=1, max_length=32767)
    replacement: str = Field(min_length=1, max_length=32767)


class ReportChartEdit(BaseModel):
    """A bounded edit of one existing chart, never arbitrary code or new data."""
    model_config = ConfigDict(extra='forbid', strict=True)
    chart_id: str = Field(min_length=1)
    top_n: int | None = Field(default=None, ge=1, le=500)
    title: str | None = Field(default=None, min_length=1, max_length=200)
    color: str | None = Field(default=None, pattern=r'^#[0-9a-fA-F]{6}$')
    chart_type: Literal['bar', 'barh'] | None = None
    table_id: str | None = None
    category_column: str | None = None
    value_column: str | None = None


class ReportSectionEdit(BaseModel):
    """Patch one uniquely named existing chapter and preserve other chapters."""
    model_config = ConfigDict(extra='forbid', strict=True)
    section_heading: str = Field(min_length=1, max_length=32767)
    heading: str | None = Field(default=None, min_length=1, max_length=32767)
    text: str | None = Field(default=None, max_length=32767)
    remove: bool = False


class ReportComponentEdit(BaseModel):
    """Patch one stable report slot using a registered analysis artifact."""
    model_config = ConfigDict(extra='forbid', strict=True)
    kind: Literal['chart', 'table']
    operation: Literal['replace', 'add', 'remove', 'move'] = 'replace'
    target_id: str | None = None
    artifact_id: str | None = None
    analysis_id: str | None = None
    position: int | None = Field(default=None, ge=0, le=500)
    visible: bool | None = None


class ReportChapterOperation(BaseModel):
    """Add/update/remove/reorder a uniquely identified custom chapter."""
    model_config = ConfigDict(extra='forbid', strict=True)
    operation: Literal['add', 'update', 'remove', 'move']
    target_heading: str | None = None
    section: ReportSection | None = None
    position: int | None = Field(default=None, ge=0, le=100)


class ReportQualityEdit(BaseModel):
    """Edit one displayed quality note; provenance fields are immutable."""
    model_config = ConfigDict(extra='forbid', strict=True)
    index: int = Field(ge=0, le=999)
    remove: bool = False
    text: str | None = Field(default=None, max_length=32767)


class ReportRevisionPatch(BaseModel):
    """General report revision; absent fields inherit, explicit empty lists clear."""
    model_config = ConfigDict(extra='forbid', strict=True)
    components: list[ReportComponentEdit] = Field(default_factory=list, max_length=100)
    sections: list[ReportChapterOperation] = Field(default_factory=list, max_length=100)
    quality_edits: list[ReportQualityEdit] = Field(default_factory=list, max_length=200)
    summary: str | None = Field(default=None, max_length=32767)
    findings: list[str] | None = Field(default=None, max_length=200)
    limitations: list[str] | None = Field(default=None, max_length=200)


class ReportLayout(BaseModel):
    """Presentation choices are independent of validated data and prose fallback."""
    model_config = ConfigDict(extra='forbid', strict=True)
    include_conclusion: bool = True
    include_metrics_summary: bool = False
    include_findings: bool = True
    include_tables: bool = False
    include_charts: bool = True
    include_quality_and_limitations: bool = True
    include_sources: bool = True

class ReportResult(Record):
    status: Literal['completed', 'failed', 'need_analysis'] = 'completed'
    report_ids: list[str] = Field(default_factory=list)
    analysis_ids: list[str] = Field(default_factory=list)
    validation_summary: dict[str, Any] = Field(default_factory=dict)
    limitations: list[str] = Field(default_factory=list)
    supplement_request: str | None = None

class MemoryEntry(Record):
    memory_id: str = Field(default_factory=new_id)
    owner: Literal['analyst', 'report_designer']
    kind: str = 'preference'
    content: str = Field(min_length=1, max_length=5000)
    scope: str = 'project'
    source_task_id: str | None = None
    status: Literal['candidate', 'active', 'deleted'] = 'candidate'
    created_at: str = Field(default_factory=utcnow)
    updated_at: str = Field(default_factory=utcnow)

AgentMemoryEntry = MemoryEntry
