"""Check local YAML against an Infrahub branch without loading it."""

import asyncio
import sys
from pathlib import Path
from typing import Annotated

import typer
import yaml
from infrahub_sdk import InfrahubClient

app = typer.Typer(help=__doc__, add_completion=False, pretty_exceptions_enable=False)


async def check(branch: str, directory: Path) -> int:
    files = sorted([*directory.glob("*.yml"), *directory.glob("*.yaml")])
    if not files:
        raise ValueError(f"No schema YAML files found in {directory}")
    client = InfrahubClient()
    schemas = []
    for path in files:
        data = yaml.safe_load(path.read_text())
        if not isinstance(data, dict):
            raise ValueError(f"{path}: expected a YAML mapping")
        client.schema.validate(data=data)
        schemas.append(data)
    success, response = await client.schema.check(schemas=schemas, branch=branch)
    if not success or response is None:
        print("Schema check rejected by Infrahub:", file=sys.stderr)
        print(yaml.safe_dump(response or {}, sort_keys=False), file=sys.stderr)
        return 1
    print(f"Schema check passed on branch {branch} ({len(files)} files).")
    print(yaml.safe_dump(response, sort_keys=False))
    return 0


@app.command()
def main(
    branch: Annotated[str, typer.Option(help="Infrahub branch to check")],
    schemas: Annotated[
        Path, typer.Option(help="Directory containing schema YAML")
    ] = Path("schemas"),
) -> None:
    try:
        code = asyncio.run(check(branch, schemas))
    except Exception as exc:
        detail = str(exc) if type(exc) is ValueError else type(exc).__name__
        print(f"Schema check failed: {detail}", file=sys.stderr)
        code = 1
    raise typer.Exit(code=code)


if __name__ == "__main__":
    app()
