"""Shared CLI option annotations for command registration."""

from pathlib import Path
from typing import Annotated
import typer

HoursOption = Annotated[int, typer.Option("--hours", help="Event-timeline look-back window")]

HtmlFlag = Annotated[
    bool, typer.Option("--html", help="Write an offline HTML snapshot to ~/vmware-health/")
]

HtmlPathOption = Annotated[
    Path | None,
    typer.Option("--html-path", help="Write the HTML snapshot to this exact path (implies --html)"),
]
