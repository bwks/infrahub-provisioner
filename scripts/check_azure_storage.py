"""Read-only validation of Azure Blob Storage and Terraform backend intent."""

import re
import unicodedata
from typing import Annotated

import typer
from infrahub_sdk import InfrahubClientSync

app = typer.Typer(help=__doc__, add_completion=False, pretty_exceptions_enable=False)
ACCOUNT = "AzureStorageAccount"
CONTAINER = "AzureBlobContainer"
BACKEND = "AzureTerraformStateBackend"
ACCOUNT_RE = r"^[a-z0-9]{3,24}\Z"
CONTAINER_RE = r"^[a-z0-9]+(?:-[a-z0-9]+)*\Z"
SKUS = (
    "Standard_LRS",
    "Standard_ZRS",
    "Standard_GRS",
    "Standard_RAGRS",
    "Standard_GZRS",
    "Standard_RAGZRS",
)
CHOICES = {
    "account_kind": ("StorageV2",),
    "sku": SKUS,
    "access_tier": ("Hot",),
    "minimum_tls_version": ("TLS1_2",),
}
BOOLEANS = {
    "public_network_access_enabled": True,
    "https_only": True,
    "allow_blob_public_access": False,
    "allow_shared_key_access": False,
    "blob_versioning_enabled": True,
}
RETENTIONS = ("blob_delete_retention_days", "container_delete_retention_days")
FIELDS = {
    ACCOUNT: (
        ["name", *CHOICES, *BOOLEANS, *RETENTIONS],
        ["resourcegroup", "location"],
    ),
    CONTAINER: (["name", "public_access"], ["storage_account"]),
    BACKEND: (["name", "key", "authentication"], ["container"]),
    "AzureResourceGroup": (["name"], ["subscription"]),
    "AzureSubscription": (["name"], ["tenant"]),
    "AzureTenant": (["name"], []),
    "AzureRegion": (["name"], []),
}


def valid_blob_key(key):
    # Flat namespace: at most 254 path segments. Reject discouraged trailing
    # separators/dots and non-URL control/surrogate characters in state keys.
    return (
        isinstance(key, str)
        and 1 <= len(key) <= 1024
        and len(key.split("/")) <= 254
        and not key.endswith((".", "/", "\\"))
        and not any(
            unicodedata.category(c) in {"Cc", "Cs"}
            or ord(c) & 0xFFFF in {0xFFFE, 0xFFFF}
            for c in key
        )
    )


def validate(inventory):
    indexes = {k: {n["id"]: n for n in inventory.get(k, [])} for k in FIELDS}
    findings = []
    names, destinations = {}, {}

    def issue(kind, n, message):
        findings.append(f"{kind} {n.get('name')} ({n['id']}): {message}")

    for kind, (_, peers) in FIELDS.items():
        targets = {
            ACCOUNT: {"resourcegroup": "AzureResourceGroup", "location": "AzureRegion"},
            CONTAINER: {"storage_account": ACCOUNT},
            BACKEND: {"container": CONTAINER},
            "AzureResourceGroup": {"subscription": "AzureSubscription"},
            "AzureSubscription": {"tenant": "AzureTenant"},
        }.get(kind, {})
        for n in inventory.get(kind, []):
            for field in peers:
                if n.get(field) not in indexes[targets[field]]:
                    issue(
                        kind, n, f"{field}: missing {targets[field]} ({n.get(field)})"
                    )
            name = n.get("name")
            if kind in {ACCOUNT, CONTAINER}:
                pattern = ACCOUNT_RE if kind == ACCOUNT else CONTAINER_RE
                maximum = 24 if kind == ACCOUNT else 63
                if (
                    not isinstance(name, str)
                    or not 3 <= len(name) <= maximum
                    or not re.fullmatch(pattern, name)
                ):
                    issue(kind, n, "invalid Azure name")
                elif (identity := (kind, n.get("storage_account"), name)) in names:
                    issue(kind, n, f"duplicate name with {names[identity]}")
                else:
                    names[(kind, n.get("storage_account"), name)] = n["id"]
            if kind == ACCOUNT:
                for field, choices in CHOICES.items():
                    if n.get(field) not in choices:
                        issue(kind, n, f"unsupported {field}: {n.get(field)!r}")
                for field in BOOLEANS:
                    if type(n.get(field)) is not bool:
                        issue(kind, n, f"{field} must be Boolean")
                for field in RETENTIONS:
                    value = n.get(field)
                    if value is not None and (
                        type(value) is not int or not 1 <= value <= 365
                    ):
                        issue(
                            kind, n, f"{field} must be null or an integer from 1 to 365"
                        )
            elif kind == CONTAINER and n.get("public_access") != "private":
                issue(kind, n, "container access must be private")
            elif kind == BACKEND:
                if not isinstance(name, str) or not 1 <= len(name) <= 128:
                    issue(kind, n, "descriptive name must contain 1–128 characters")
                key = n.get("key")
                if not valid_blob_key(key):
                    issue(
                        kind,
                        n,
                        "invalid state blob key (length, path segments, or characters)",
                    )
                else:
                    identity = (n.get("container"), key)
                    if identity in destinations:
                        issue(
                            kind,
                            n,
                            f"duplicate backend destination with {destinations[identity]}",
                        )
                    destinations[identity] = n["id"]
                if n.get("authentication") != "entra_id":
                    issue(kind, n, "backend authentication must use Microsoft Entra ID")
                container = indexes[CONTAINER].get(n.get("container"))
                account = (
                    indexes[ACCOUNT].get(container.get("storage_account"))
                    if container
                    else None
                )
                if account:
                    for field, expected in {
                        "https_only": True,
                        "allow_blob_public_access": False,
                        "allow_shared_key_access": False,
                    }.items():
                        if account.get(field) is not expected:
                            issue(
                                kind,
                                n,
                                f"storage account {account['id']}: {field} must be {expected}",
                            )
    return findings


def check(client, branch):
    inventory = {}
    for kind, (attrs, peers) in FIELDS.items():
        inventory[kind] = [
            {
                "id": n.id,
                **{a: getattr(n, a).value for a in attrs},
                **{p: getattr(n, p).id for p in peers},
            }
            for n in client.all(
                kind=kind, branch=branch, include=attrs + peers, populate_store=False
            )
        ]
    findings = validate(inventory)
    for finding in findings:
        typer.echo(finding, err=True)
    count = sum(len(inventory[k]) for k in (ACCOUNT, CONTAINER, BACKEND))
    if findings:
        typer.echo(
            f"Azure storage invalid on branch {branch}: {len(findings)} findings.",
            err=True,
        )
        return 1
    typer.echo(
        f"Azure storage valid on branch {branch}: {count} storage/backend objects."
        + (" Storage inventory is empty." if not count else "")
    )
    return 0


@app.command()
def main(branch: Annotated[str, typer.Option(help="Infrahub branch to validate")]):
    try:
        code = check(InfrahubClientSync(), branch)
    except Exception as exc:
        typer.echo(f"Azure storage read failed: {type(exc).__name__}", err=True)
        code = 1
    raise typer.Exit(code=code)


if __name__ == "__main__":
    app()
