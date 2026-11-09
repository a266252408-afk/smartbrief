"""审核与返工输出的严格契约。"""
import json
import re
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
Text = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1)]
Positive = Annotated[int, Field(strict=True, ge=1)]
class Contract(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
class Location(Contract):
    paragraph: Positive
    sentence: Positive
class SourceEvidence(Contract):
    kind: Literal['source'] = 'source'
    source_id: Text
    quote: Text
class ReportEvidence(Contract):
    kind: Literal['report']
    quote: Text
class Finding(Contract):
    finding_id: Text
    problem: Text
    expected: Text
    severity: Literal['hard','soft']
    evidence: SourceEvidence | ReportEvidence
    location: Location
    scope: Literal['paragraph', 'full'] = 'paragraph'
class CoreCoverage(Contract):
    question: Text
    covered: bool
    quote: Text
    reason: Text
class SemanticDuplicate(Contract):
    first_quote: Text
    second_quote: Text
    information_loss: bool
    reason: Text
class ReviewResult(Contract):
    passed: bool
    comments: list[Text]
    findings: list[Finding] = Field(default_factory=list)
    possible_rule_issue: Text | None = None
    full_rewrite_required: bool = False
    core_coverage: list[CoreCoverage] = Field(default_factory=list)
    semantic_duplicates: list[SemanticDuplicate] = Field(default_factory=list)
    @model_validator(mode='after')
    def check_result(self):
        if not self.passed and not self.comments:
            raise ValueError('拒绝审核必须说明原因')
        ids = [f.finding_id for f in self.findings]
        if len(ids) != len(set(ids)):
            raise ValueError('finding_id 重复')
        return self
class Receipt(Contract):
    finding_id: Text
    status: Literal['fixed','skipped','cannot_fix']
    reason: str = ''
    @model_validator(mode='after')
    def check_reason(self):
        if self.status != 'fixed' and not self.reason.strip():
            raise ValueError('未修复回执必须说明原因')
        return self
class Patch(Contract):
    paragraph: Positive
    digest: Text
    replacement: str
    finding_ids: list[Text]
class CitationChange(Contract):
    source_id: Text
    original_claim: Text
    action: Literal['deleted']
class PatchResult(Contract):
    mode: Literal['patch']
    patches: list[Patch]
    results: list[Receipt]
    citation_changes: list[CitationChange] = Field(default_factory=list)
class FullResult(Contract):
    mode: Literal['full']
    content: Text
    reason: Text
    results: list[Receipt]
    citation_changes: list[CitationChange] = Field(default_factory=list)
def parse_json(raw):
    text = (raw or '').strip()
    fence = re.fullmatch(r'```(?:json)?\s*(.*?)\s*```', text, re.DOTALL)
    return json.loads(fence.group(1) if fence else text)
def response_schema(model):
    schema = model.model_json_schema()
    def strict(node):
        if isinstance(node, dict):
            if node.get('type') == 'object':
                node['required'] = list(node.get('properties', {}))
                node['additionalProperties'] = False
            node.pop('default', None)
            for value in node.values():
                strict(value)
        elif isinstance(node, list):
            for value in node:
                strict(value)
    strict(schema)
    return schema
