"""Synthetic document-control records and content locations."""

from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import quote

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
PDF = "application/pdf"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
HTML = "text/html"
TEXT = "text/plain"
SCENARIOS = ("baseline", "rollout", "fault")


@dataclass(frozen=True)
class Record:
    doc_id: str
    revision: str
    source: str
    key: str
    media_type: str
    lifecycle: str
    roles: tuple[str, ...]
    blob: str | None = None

    @property
    def path(self) -> Path:
        return FIXTURES / self.source / (self.blob or self.key)

    @property
    def content_path(self) -> str:
        return f"/{self.source}/content/{quote(self.key, safe='')}"

    def as_json(self) -> dict:
        return {
            "doc_id": self.doc_id,
            "version": self.revision,
            "source": self.source,
            "filename": self.key,
            "media_type": self.media_type,
            "lifecycle": self.lifecycle,
            "allowed_roles": list(self.roles),
            "content_path": self.content_path,
        }


DISPATCH = ("dispatch", "supervisor")
SUPPORT = ("support", "supervisor")
BASELINE = (
    Record("DSP-104", "1.8", "object-store", "West Cutoffs FINAL 2028.pdf", PDF, "deprecated", DISPATCH),
    Record("DSP-104", "2.4", "object-store", "west_cutoff_copy.pdf", PDF, "active", DISPATCH),
    Record("DSP-104", "2.6", "object-store", "weeknight draft.pdf", PDF, "draft", DISPATCH),
    Record("CLM-220", "3.1", "portal", "Claims intake v3 FINAL.docx", DOCX, "active", SUPPORT),
    Record("ESC-901", "2.1", "portal", "Exception desk note.html", HTML, "active", ("finance",)),
    Record("RET-412", "1.2", "portal", "Returns old intranet.html", HTML, "active", SUPPORT),
    Record("HUB-307", "4.2", "object-store", "sort_floor_export.pdf", PDF, "active", DISPATCH),
    Record("LBL-118", "1.0", "portal", "Label FAQ.docx", DOCX, "active", SUPPORT),
    Record("CLS-117", "5.0", "object-store", "dock_close.txt", TEXT, "active", DISPATCH),
    Record("HUB-355", "1.3", "object-store", "IMG_1087.pdf", PDF, "active", DISPATCH),
    Record("HUB-731", "1.0", "object-store", "handoff_attachment.pdf", PDF, "active", DISPATCH),
)

REPLACEMENT = Record("DSP-104", "2.5", "object-store", "dispatch bulletin nov.pdf", PDF, "active", DISPATCH)
UNREGISTERED = Record("DSP-104", "9.9", "object-store", "unregistered planning export.pdf", PDF, "draft", DISPATCH)


def records_for(scenario: str) -> tuple[Record, ...]:
    if scenario == "baseline":
        return BASELINE

    changes = {
        ("DSP-104", "2.4"): {"lifecycle": "deprecated"},
        ("CLM-220", "3.1"): {"roles": ("supervisor",)},
        ("RET-412", "1.2"): {"lifecycle": "withdrawn"},
        ("LBL-118", "1.0"): {"blob": "Label FAQ rollout.docx"},
    }
    replacement = REPLACEMENT
    if scenario == "fault":
        changes.pop(("DSP-104", "2.4"))
        replacement = replace(REPLACEMENT, lifecycle="deprecated")
    return tuple(
        replace(record, **changes.get((record.doc_id, record.revision), {}))
        for record in BASELINE
    ) + (replacement,)
