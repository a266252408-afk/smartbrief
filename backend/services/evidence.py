"""非可信搜索结果转为任务内证据；不推断事实正确性。"""
from datetime import datetime, timezone
import re
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator
from services.evidence_excerpt import select_excerpt


def canonical_url(value: str) -> str:
    if not isinstance(value, str) or any(c.isspace() or ord(c) < 32 for c in value) or "\\" in value:
        raise ValueError("来源URL含非法字符")
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise ValueError("来源仅允许无凭证HTTP(S)URL")
    port = parsed.port
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    if port and (parsed.scheme.lower(), port) not in {("http", 80), ("https", 443)}:
        host += f":{port}"
    return urlunsplit((parsed.scheme.lower(), host, parsed.path or "/", parsed.query, ""))


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    source_id: str = Field(pattern=r"^S[1-9][0-9]*$")
    task_id: str = Field(min_length=1)
    kind: Literal["web", "document"]
    title: str = Field(min_length=1, max_length=500)
    excerpt: str = Field(min_length=1, max_length=4000)
    retrieved_at: str
    url: str | None = None
    document_id: str | None = None
    chunk_id: str | None = None
    page: int | None = Field(default=None, ge=1)
    score: float | None = Field(default=None, allow_inf_nan=False)
    source_deleted: StrictBool = False
    excerpt_status: Literal["unverified", "boundary_selected", "incomplete_tail"] = "unverified"

    @field_validator("title", "excerpt")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("证据字段不能为空白")
        return value

    @field_validator("retrieved_at")
    @classmethod
    def timestamp(cls, value):
        if datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo is None:
            raise ValueError("证据时间必须包含时区")
        return value

    @model_validator(mode="after")
    def check_origin(self):
        if self.kind == "web":
            self.url = canonical_url(self.url)
        elif not self.document_id or not self.chunk_id:
            raise ValueError("文档证据需要document_id与chunk_id")
        if self.url is not None:
            self.url = canonical_url(self.url)
        return self


def validate_sources(task_id: str, sources: list) -> list[dict]:
    if not isinstance(sources, list):
        raise ValueError("证据必须为列表")
    result, ids, origins = [], set(), set()
    for item in sources:
        model = Evidence.model_validate(item)
        origin = (model.kind, model.url) if model.kind == "web" else (model.kind, model.document_id, model.chunk_id)
        if model.task_id != task_id or model.source_id in ids or origin in origins:
            raise ValueError("证据跨任务或编号/来源重复")
        ids.add(model.source_id)
        origins.add(origin)
        result.append(model.model_dump())
    return result


def normalize_evidence(task_id: str, research_data: dict, existing=None) -> list[dict]:
    if not task_id or not isinstance(research_data, dict):
        return []
    sources = validate_sources(task_id, existing if existing is not None else [])
    urls = {item["url"] for item in sources if item["kind"] == "web"}
    next_id = max((int(item["source_id"][1:]) for item in sources), default=0) + 1
    for value in research_data.values():
        if not isinstance(value, dict) or not isinstance(value.get("observations"), list):
            continue
        for observation in value["observations"]:
            result = observation.get("result") if isinstance(observation, dict) else None
            if not isinstance(result, dict) or "error" in result or not isinstance(result.get("results"), list):
                continue
            for item in result["results"]:
                if not isinstance(item, dict) or not isinstance(item.get("snippet", ""), str):
                    continue
                try:
                    model = Evidence(
                        source_id=f"S{next_id}", task_id=task_id, kind="web",
                        title=item.get("title", "")[:500], excerpt=select_excerpt(item.get("snippet", ""), 4000),
                        excerpt_status=("incomplete_tail" if re.search(r"\d[\d.,]*$", item.get("snippet", "").strip())
                                        else "boundary_selected" if len(item.get("snippet", "")) > 4000 else "unverified"),
                        url=item.get("url"),
                        retrieved_at=result.get("retrieved_at") or datetime.now(timezone.utc).isoformat(),
                    )
                except (ValueError, TypeError):
                    continue
                if model.url in urls:
                    for index, old in enumerate(sources):
                        if (old.get("url") == model.url and old.get("excerpt_status") == "incomplete_tail"
                                and model.excerpt.startswith(old["excerpt"]) and model.excerpt != old["excerpt"]):
                            sources[index] = dict(model.model_dump(), source_id=old["source_id"])
                            break
                    continue
                sources.append(model.model_dump())
                urls.add(model.url)
                next_id += 1
    return sources
