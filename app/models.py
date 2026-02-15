from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field


class VoiceCreate(BaseModel):
    """Schema for creating a new voice."""

    name: str = Field(..., min_length=1, max_length=100)
    style_instruction: str = Field(..., min_length=10)
    reference_text: str = Field(..., min_length=10)


class Voice(BaseModel):
    """Schema for a voice in the database."""

    id: str = Field(..., alias="_id")
    name: str
    slug: str
    style_instruction: str
    reference_text: str
    audio_filename: str
    created_at: datetime

    class Config:
        populate_by_name = True


class BroadcastCreate(BaseModel):
    """Schema for creating a new broadcast."""

    voice_id: str
    input_text: str = Field(..., min_length=10)
    twist: Optional[str] = None
    style_override: Optional[str] = None
    target_minutes: int = Field(default=4, ge=1, le=20)
    generate_in_chunks: bool = True
    chunk_minutes: float = Field(default=1.0, ge=0.25, le=10.0)


class Broadcast(BaseModel):
    """Schema for a broadcast in the database."""

    id: str = Field(..., alias="_id")
    title: str
    voice_id: str
    voice_name: Optional[str] = None  # Populated from join
    topic: str
    twist: Optional[str] = None
    style_override: Optional[str] = None
    input_text: str
    generated_script: str
    summary: Optional[str] = None
    audio_filename: str
    duration_seconds: int
    target_minutes: Optional[int] = None
    generate_in_chunks: Optional[bool] = None
    chunk_minutes: Optional[float] = None
    created_at: datetime

    class Config:
        populate_by_name = True


class GenerationResult(BaseModel):
    """Result of a broadcast generation."""

    success: bool
    broadcast_id: Optional[str] = None
    error: Optional[str] = None
    audio_url: Optional[str] = None
    title: Optional[str] = None
    summary: Optional[str] = None
    duration_seconds: Optional[int] = None
