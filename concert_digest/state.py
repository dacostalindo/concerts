"""Small JSON state file: featured concerts, the watchlist, processed replies."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class Watch:
    event_id: str
    artist: str
    title: str
    date: str
    venue: str
    city: str
    added: str
    concertje_url: str = ""
    ticket_url: str = ""
    on_sale_at: str = ""
    notified_heads_up: bool = False
    notified_on_sale: bool = False


@dataclass
class State:
    path: Path
    featured: dict[str, dict] = field(default_factory=dict)  # event id -> {"sent": date, "artist": name}
    watch: dict[str, Watch] = field(default_factory=dict)
    processed_messages: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, home: Path) -> "State":
        path = home / "state.json"
        if not path.exists():
            return cls(path)
        raw = json.loads(path.read_text())
        return cls(
            path=path,
            featured=raw.get("featured", {}),
            watch={k: Watch(**v) for k, v in raw.get("watch", {}).items()},
            processed_messages=raw.get("processed_messages", []),
        )

    def save(self) -> None:
        data = {
            "featured": self.featured,
            "watch": {k: asdict(v) for k, v in self.watch.items()},
            "processed_messages": self.processed_messages[-500:],
        }
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".state-")
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, self.path)
