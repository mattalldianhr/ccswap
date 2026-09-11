"""The job form: one modal for creating and editing a job.

Enumerated fields (account, model, effort, permission mode) are ``Select``
widgets; the prompt is a multi-line ``TextArea``; everything else is an
``Input``. Validation is the backend's :func:`~claude_swap.jobs.validate_job_fields`,
so the form can never queue something the CLI would reject.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Select, Static, TextArea

from claude_swap.jobs import (
    ACCOUNT_AUTO,
    EFFORT_LEVELS,
    PERMISSION_MODES,
    Job,
    new_job_id,
    validate_job_fields,
)
from claude_swap.mappings import normalize_path
from claude_swap.settings import JobsSettings

MODEL_CHOICES: tuple[tuple[str, str], ...] = (
    ("Claude Code default", ""),
    ("sonnet", "sonnet"),
    ("opus", "opus"),
    ("fable", "fable"),
    ("custom…", "__custom__"),
)
EFFORT_CHOICES: tuple[tuple[str, str], ...] = (("default", ""),) + tuple((e, e) for e in EFFORT_LEVELS)
PERMISSION_HELP = {
    "acceptEdits": "edits files without asking; commands still need allowed tools",
    "auto": "Claude Code's auto mode: a classifier approves routine actions",
    "bypassPermissions": "runs any command unattended — use only in folders you trust",
    "manual": "asks for everything; a headless job will stall on the first prompt",
    "dontAsk": "denies anything not pre-allowed instead of asking",
    "plan": "read-only: plans, never edits or runs",
}


@dataclass(frozen=True)
class JobForm:
    """What the modal collects; ``job_id`` is set when editing."""

    job_id: str | None
    name: str
    folder: str
    prompt: str
    account: str
    model: str | None
    effort: str | None
    permission_mode: str
    allowed_tools: tuple[str, ...]
    max_turns: int | None
    priority: int
    estimate_pct: float
    auto: bool

    def fields(self) -> dict:
        return {
            "name": self.name, "folder": self.folder, "prompt": self.prompt,
            "account": self.account, "model": self.model, "effort": self.effort,
            "permission_mode": self.permission_mode, "allowed_tools": self.allowed_tools,
            "max_turns": self.max_turns, "priority": self.priority,
            "estimate_pct": self.estimate_pct, "auto": self.auto,
        }

    def to_job(self) -> Job:
        return Job(id=new_job_id(), **self.fields())


def split_tools(raw: str) -> tuple[str, ...]:
    """Split ``Bash(git *) Edit Write`` on whitespace outside parentheses."""
    out: list[str] = []
    buf: list[str] = []
    depth = 0
    for ch in raw:
        if ch == "(":
            depth += 1
        elif ch == ")" and depth:
            depth -= 1
        if ch.isspace() and depth == 0:
            if buf:
                out.append("".join(buf))
                buf = []
            continue
        buf.append(ch)
    if buf:
        out.append("".join(buf))
    return tuple(out)


def _derive_name(prompt: str) -> str:
    words = [w for w in prompt.strip().split() if w]
    return "-".join(w.lower().strip(".,:;!?\"'`") for w in words[:4])[:40] or "job"


class JobFormModal(ModalScreen["JobForm | None"]):
    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("ctrl+s", "submit", "Queue", show=False),
    ]

    def __init__(
        self,
        job: Job | None,
        *,
        settings: JobsSettings,
        default_folder: str,
        accounts: list[tuple[str, str]],
    ) -> None:
        super().__init__()
        self._job = job
        self._settings = settings
        self._default_folder = default_folder
        self._accounts = accounts

    # -- compose ----------------------------------------------------------------

    def compose(self) -> ComposeResult:
        job = self._job
        s = self._settings
        model = job.model if job else (s.default_model or "")
        model_value = model if model in {v for _l, v in MODEL_CHOICES} else ("__custom__" if model else "")
        effort = (job.effort if job else s.default_effort) or ""
        mode = job.permission_mode if job else s.default_permission_mode
        account = job.account if job else ACCOUNT_AUTO
        account_values = {v for _l, v in self._accounts}
        if account not in account_values:
            self._accounts = [*self._accounts, (f"#{account}", account)]
        with Vertical(classes="modal-box modal-box-wide", id="jobform-box"):
            yield Label("Edit job" if job else "New job", classes="modal-title")
            with VerticalScroll(id="jobform-scroll"):
                yield Label("folder", classes="form-label")
                yield Input(job.folder if job else self._default_folder, id="f-folder", placeholder="~/path/to/repo")
                yield Label("name", classes="form-label")
                yield Input(job.name if job else "", id="f-name", placeholder="(derived from the prompt)")
                yield Label("prompt", classes="form-label")
                yield TextArea(job.prompt if job else "", id="f-prompt", tab_behavior="focus", show_line_numbers=False)
                with Horizontal(classes="form-row"):
                    with Vertical(classes="form-col"):
                        yield Label("account", classes="form-label")
                        yield Select(self._accounts, value=account, allow_blank=False, id="f-account")
                    with Vertical(classes="form-col"):
                        yield Label("model", classes="form-label")
                        yield Select(list(MODEL_CHOICES), value=model_value, allow_blank=False, id="f-model")
                yield Input(model if model_value == "__custom__" else "", id="f-model-custom",
                            placeholder="full model id, e.g. claude-opus-5")
                with Horizontal(classes="form-row"):
                    with Vertical(classes="form-col"):
                        yield Label("effort", classes="form-label")
                        yield Select(list(EFFORT_CHOICES), value=effort, allow_blank=False, id="f-effort")
                    with Vertical(classes="form-col"):
                        yield Label("permissions", classes="form-label")
                        yield Select([(m, m) for m in PERMISSION_MODES], value=mode, allow_blank=False, id="f-mode")
                yield Static(PERMISSION_HELP.get(mode, ""), id="f-mode-help", classes="form-help")
                yield Label("allowed tools (space-separated, optional)", classes="form-label")
                yield Input(" ".join(job.allowed_tools) if job else "", id="f-tools", placeholder='Bash(git *) Edit Write')
                with Horizontal(classes="form-row"):
                    with Vertical(classes="form-col"):
                        yield Label("max turns", classes="form-label")
                        yield Input(str(job.max_turns) if job and job.max_turns else "", id="f-turns", type="integer", placeholder="none")
                    with Vertical(classes="form-col"):
                        yield Label("priority (lower first)", classes="form-label")
                        yield Input(str(job.priority if job else 50), id="f-priority", type="integer")
                    with Vertical(classes="form-col"):
                        yield Label("estimate, % of 5h", classes="form-label")
                        yield Input(f"{(job.estimate_pct if job else s.default_estimate_pct):g}", id="f-estimate", type="number")
                yield Label("start", classes="form-label")
                yield Select(
                    [("when there is spare capacity", "auto"), ("only by hand", "manual")],
                    value="auto" if (job.auto if job else True) else "manual",
                    allow_blank=False, id="f-auto",
                )
            yield Static("", id="form-error", classes="form-error")
            with Horizontal(classes="modal-buttons"):
                yield Button("Save" if job else "Queue", id="submit")
                yield Button("Cancel", id="cancel")
            yield Static("tab next · shift-tab back · ctrl-s queue · esc cancel", classes="modal-hint")

    def on_mount(self) -> None:
        self._sync_custom_model()
        self.query_one("#f-prompt" if self._job is None and self._default_folder else "#f-folder").focus()

    # -- reactions --------------------------------------------------------------

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "f-model":
            self._sync_custom_model()
        elif event.select.id == "f-mode":
            self.query_one("#f-mode-help", Static).update(PERMISSION_HELP.get(str(event.value), ""))

    def _sync_custom_model(self) -> None:
        custom = self.query_one("#f-model-custom", Input)
        custom.display = self.query_one("#f-model", Select).value == "__custom__"

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
        else:
            self.action_submit()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.action_submit()

    def action_cancel(self) -> None:
        self.dismiss(None)

    # -- submit -----------------------------------------------------------------

    def _error(self, message: str, focus: str | None = None) -> None:
        self.query_one("#form-error", Static).update(message)
        if focus:
            self.query_one(focus).focus()

    def action_submit(self) -> None:
        folder_raw = self.query_one("#f-folder", Input).value.strip()
        if not folder_raw:
            self._error("Folder is required.", "#f-folder")
            return
        try:
            folder = normalize_path(folder_raw)
        except OSError:
            self._error(f"Bad folder: {folder_raw}", "#f-folder")
            return
        prompt = self.query_one("#f-prompt", TextArea).text
        name = self.query_one("#f-name", Input).value.strip() or _derive_name(prompt)
        account = str(self.query_one("#f-account", Select).value)
        model_sel = str(self.query_one("#f-model", Select).value)
        if model_sel == "__custom__":
            model = self.query_one("#f-model-custom", Input).value.strip() or None
            if model is None:
                self._error("Enter a model id or pick one from the list.", "#f-model-custom")
                return
        else:
            model = model_sel or None
        effort = str(self.query_one("#f-effort", Select).value) or None
        mode = str(self.query_one("#f-mode", Select).value)
        tools = split_tools(self.query_one("#f-tools", Input).value)
        turns_raw = self.query_one("#f-turns", Input).value.strip()
        try:
            max_turns = int(turns_raw) if turns_raw else None
            priority = int(self.query_one("#f-priority", Input).value.strip() or 50)
            estimate = float(self.query_one("#f-estimate", Input).value.strip() or self._settings.default_estimate_pct)
        except ValueError:
            self._error("Turns, priority and estimate must be numbers.", "#f-turns")
            return
        problem = validate_job_fields(
            folder=folder, prompt=prompt, permission_mode=mode, effort=effort,
            max_turns=max_turns, estimate_pct=estimate, priority=priority,
        )
        if problem:
            focus = "#f-folder" if problem.startswith("Folder") else "#f-prompt" if problem.startswith("Prompt") else None
            self._error(problem, focus)
            return
        auto = str(self.query_one("#f-auto", Select).value) == "auto"
        self.dismiss(JobForm(
            job_id=self._job.id if self._job else None,
            name=name, folder=folder, prompt=prompt, account=account,
            model=model, effort=effort, permission_mode=mode, allowed_tools=tools,
            max_turns=max_turns, priority=priority, estimate_pct=estimate, auto=auto,
        ))
