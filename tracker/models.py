from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class Observation:
    timestamp: datetime
    app_name: str
    window_title: Optional[str] = None
    browser_url: Optional[str] = None
    browser_tab_title: Optional[str] = None
    is_idle: bool = False
    screenshot_path: Optional[str] = None
    classified: bool = False
    id: Optional[int] = None


@dataclass
class ActivitySpan:
    start_time: datetime
    end_time: datetime
    duration_seconds: int
    app_name: str
    window_title: Optional[str] = None
    browser_url: Optional[str] = None
    category: Optional[str] = None
    subcategory: Optional[str] = None
    description: Optional[str] = None
    observation_count: int = 1
    id: Optional[int] = None
    observation_ids: list[int] = field(default_factory=list)


@dataclass
class WebPage:
    url: str
    content: str
    captured_at: datetime
    title: Optional[str] = None
    content_length: int = 0
    observation_id: Optional[int] = None
    id: Optional[int] = None


@dataclass
class Person:
    name: str
    canonical_name: str
    first_seen: datetime
    last_seen: datetime
    interaction_count: int = 1
    email: Optional[str] = None
    organization: Optional[str] = None
    id: Optional[int] = None
