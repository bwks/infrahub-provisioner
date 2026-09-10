"""Read-only validation of Azure tag assignments; does not write to Infrahub or Azure."""

from collections import Counter
from dataclasses import dataclass
from typing import Annotated

import typer
from infrahub_sdk import InfrahubClientSync

app = typer.Typer(help=__doc__, add_completion=False, pretty_exceptions_enable=False)
OWNER_KINDS = (
    "AzureSubscription",
    "AzureResourceGroup",
    "AzureVirtualNetwork",
    "AzureNetworkSecurityGroup",
    "AzureRouteTable",
    "AzureStorageAccount",
)
INVALID_KEY_CHARACTERS = set("<>%&\\?/")


@dataclass(frozen=True)
class Tag:
    id: str
    key: str | None
    value: str | None
    owner: str | None


def validate(tags: list[Tag], owners: dict[str, str]) -> list[str]:
    findings = []
    seen = {}
    counts = Counter()
    for tag in tags:
        label = f"Tag {tag.id} (owner {tag.owner}, key {tag.key!r})"
        if tag.owner not in owners or owners[tag.owner] not in OWNER_KINDS:
            findings.append(f"{label}: missing or unsupported owner")
        counts[tag.owner] += 1
        if not isinstance(tag.key, str) or not 1 <= len(tag.key) <= 512:
            findings.append(f"{label}: key must contain 1–512 characters")
        elif INVALID_KEY_CHARACTERS.intersection(tag.key):
            findings.append(f"{label}: key contains prohibited characters")
        if isinstance(tag.key, str):
            identity = (tag.owner, tag.key.casefold())
            if identity in seen:
                findings.append(
                    f"{label}: duplicate key (case-insensitive) with tag {seen[identity]}"
                )
            seen[identity] = tag.id
        if not isinstance(tag.value, str) or len(tag.value) > 256:
            findings.append(f"{label}: value must be text of at most 256 characters")
    for owner, count in counts.items():
        if count > 50:
            findings.append(f"Owner {owner}: {count} tags exceeds the limit of 50")
    return findings


def check(client, branch: str) -> int:
    owners = {
        n.id: n.get_kind()
        for n in client.all(kind="AzureTaggable", branch=branch, populate_store=False)
    }
    tags = [
        Tag(n.id, n.key.value, n.value.value, n.owner.id)
        for n in client.all(
            kind="AzureTag",
            branch=branch,
            include=["key", "value", "owner"],
            populate_store=False,
        )
    ]
    findings = validate(tags, owners)
    for finding in findings:
        typer.echo(finding, err=True)
    if findings:
        typer.echo(
            f"Azure tags invalid on branch {branch}: {len(findings)} findings.",
            err=True,
        )
        return 1
    typer.echo(
        f"Azure tags valid on branch {branch}: {len(tags)} tags, {len(owners)} owners."
        + (" Tag inventory is empty." if not tags else "")
    )
    return 0


@app.command()
def main(
    branch: Annotated[str, typer.Option(help="Infrahub branch to validate")],
) -> None:
    try:
        code = check(InfrahubClientSync(), branch)
    except Exception as exc:
        # SDK exceptions may include request details; report only their type.
        typer.echo(f"Azure tag read failed: {type(exc).__name__}", err=True)
        code = 1
    raise typer.Exit(code=code)


if __name__ == "__main__":
    app()
