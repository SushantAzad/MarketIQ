"""Bounded model output; final quotes and citation URLs are never supplied by the LLM."""

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

PROMPT_VERSION = "extractive-rag-v1"


class Selection(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    answerable: bool
    source_ids: list[str] = Field(max_length=3)

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.answerable != bool(self.source_ids) or len(set(self.source_ids)) != len(
            self.source_ids
        ):
            raise ValueError("Inconsistent evidence selection")
        return self
