"""Every Python example in the README runs, as written, against the stand-in
Developer API, so the README can't promise what the SDK doesn't do."""

from __future__ import annotations

import re
import textwrap
from pathlib import Path
from typing import Any

import anyio
import pytest

from lodol import Lodol
from tests.standin import KEY, Server, address

README = Path(__file__).resolve().parent.parent / "README.md"
EXAMPLES = re.findall(r"```python\n(.*?)```", README.read_text(), flags=re.S)

# The README's placeholder ids, as the stand-in knows them.
IDS = {"665f...": "665f", "683b...": "r1", "5c3d...": "5c3d", "4e8d...": "4e8d"}


def test_the_readme_has_examples() -> None:
    assert len(EXAMPLES) >= 10


@pytest.mark.parametrize(
    "example", EXAMPLES, ids=[f"example-{i + 1}" for i in range(len(EXAMPLES))]
)
def test_readme_example_runs(
    example: str, server: Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LODOL_API_KEY", KEY)
    monkeypatch.setenv("LODOL_BASE_URL", address(server))
    code = example
    for placeholder, real in IDS.items():
        code = code.replace(placeholder, real)

    with Lodol() as lodol:
        # The run the examples that follow one up refer to.
        lodol.workflows.run("665f", inputs={"client": "Acme"})
        namespace: dict[str, Any] = {"lodol": lodol}
        if "await " in code or "async " in code:
            source = "async def _example():\n" + textwrap.indent(code, "    ")
            exec(compile(source, str(README), "exec"), namespace)
            anyio.run(namespace["_example"])
        else:
            exec(compile(code, str(README), "exec"), namespace)
