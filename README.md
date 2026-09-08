# infrahub-provisioner

Configure Infrahub through code to provide a source of infrastructure intent for
Infrastructure as Code (IaC). Start with basic IP address management (IPAM), then
extend toward Azure and external Terraform/OpenTofu consumers.

## Current status

The repository provides unmodified upstream base and VRF schemas, a locked Python
environment,
an SDK-based schema check, and read-only verification of the deployed models.

The upstream base and VRF schemas are deployed on the lab’s `main` branch after
validation and merge of `upstream-ipam`. No sample or operational objects were loaded during schema setup.

The lab runs Infrahub `1.10.6`. This workflow uses SDK `1.22.1`, selected from the
documented compatible `1.22.x` series and tested against the lab with Python 3.14.
Recheck compatibility before upgrading either dependency or server.

## First milestone: IPAM schemas

The first implementation milestone is **schemas only**, covering:

- IP namespaces for isolated addressing, including overlapping address space.
- Prefixes and subnets, with subnets represented as prefixes.
- IPv4 and IPv6 addresses using Infrahub's native IPAM hierarchy.
- VRFs as routing contexts, distinct from addressing namespaces.

Use YAML with the official Python SDK and `infrahubctl`, reusing compatible models
from the OpsMill Schema Library. Pin dependencies and upstream schema revisions,
and validate schema changes on a dedicated Infrahub branch before integration.

The schemas-only milestone is complete. The seed workflow adds the reference
catalog described below; it creates no operational allocations.
Automatic allocation and relationships to Azure resources are deferred.

The unchanged upstream dependency set also supplies DCIM, organization, location
generics, and route-target schemas. These are loaded as dependencies, without
populating their objects. Upstream menu settings are preserved, including hidden
standalone prefix/address menu entries.

### Progress

- [x] Initialize the GitHub repository and project guidance.
- [x] Verify lab login and GraphQL access.
- [x] Document scope and the first milestone.
- [x] Inspect the live schema and select compatible SDK/library versions.
- [x] Establish reproducible local tooling and provisioning commands.
- [x] Vendor unmodified upstream base and VRF schemas with required dependencies.
- [x] Validate schema loading and repeatability on a dedicated Infrahub branch.
- [x] Integrate the validated schemas and document the working workflow.

## Lab access

The existing lab instance is at <http://ihub01:8000>. These default credentials are
intentionally documented with the lab owner's authorization:

```sh
export INFRAHUB_ADDRESS='http://ihub01:8000'
export INFRAHUB_USERNAME='admin'
export INFRAHUB_PASSWORD='infrahub'
```

Set these variables in the shell where you run the commands below. No persistent
API token is needed for this username/password workflow.

For future automation, the SDK supports `INFRAHUB_API_TOKEN`. Keep generated tokens
outside tracked files and logs. See [AGENTS.md](AGENTS.md) for authentication,
branch selection, API endpoints, and contributor instructions.

## Setup and schema workflow

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), clone this
repository, and run commands from its root. Python 3.12–3.14 is supported by the
project; `.python-version` selects 3.14 and uv can provision it if needed.

```sh
uv sync --locked
```

The load uses the checked-in YAML; it does not download changing upstream schemas.
The [provenance notes](third_party/schema-library/README.md) record the upstream
revision, license, and file hashes. The full upstream base includes DCIM, location,
and organization dependencies; the VRF extension includes route targets. Load the
whole `schemas` directory together so VRF extensions can
resolve the prefix and address models.

For the initial bootstrap, create a dedicated **Infrahub branch** (separate from
Git branches), check the proposed changes, then load and verify:

```sh
uv run infrahubctl branch create upstream-ipam
uv run python scripts/check_schema.py --branch upstream-ipam
uv run infrahubctl schema load schemas --branch upstream-ipam --wait 30
uv run python scripts/verify_schema.py --branch upstream-ipam
```

Inspect the check output before loading. The schema includes prefixes, addresses,
VRFs, route targets, and required base models; it reuses the built-in
`IpamNamespace`. All five YAML files are byte-for-byte upstream copies.
Use a new branch name for subsequent changes. Branch creation itself is not
idempotent; skip it if intentionally resuming an existing open validation branch.

Verify repeatability by rerunning the load and check:

```sh
uv run infrahubctl schema load schemas --branch upstream-ipam --wait 30
uv run python scripts/check_schema.py --branch upstream-ipam
```

An unchanged load reports that the schema is already up to date. The check should
show empty `added`, `changed`, and `removed` sections. After successful validation,
integrate and verify the destination:

```sh
INFRAHUB_TIMEOUT=180 uv run infrahubctl branch merge upstream-ipam
uv run python scripts/check_schema.py --branch main
uv run python scripts/verify_schema.py --branch main
```

The merge uses a longer request timeout because integration can take more than a
minute in this lab. If a request times out or says the branch is being merged,
inspect branch status and verify `main` before retrying.

The check and verification scripts require an explicit branch and return a nonzero
exit code on failure. Use `scripts/check_schema.py` for automated gates: the pinned
`infrahubctl schema check` can print server validation errors while returning exit
code zero. Schema loading still uses the official CLI. A failed load or worker
convergence timeout requires inspection of the target branch before retrying;
it does not imply that the server rolled back the load.

## Model behavior and validation

`IpamPrefix` and `IpamIPAddress` inherit Infrahub's native IPAM generics and use
namespace-scoped uniqueness. Both have status, description, and optional role
fields; addresses also have an optional FQDN. Subnets use `IpamPrefix`.

`IpamVRF` has a globally unique name, optional route distinguisher, and description.
Prefixes and addresses can optionally reference one VRF. VRFs do not create or
select namespaces, and do not independently permit duplicate addresses in a
namespace. Use separate namespaces for overlapping address space. Upstream route-target
import/export relationships and the `enforce_unique` field are retained. That field
records intent only: upstream does not enforce VRF-level uniqueness.

Validation performed against `upstream-ipam` and the merged `main` includes schema acceptance, native
inheritance, namespace-scoped uniqueness constraints, optional VRF relationships,
GraphQL availability, and an unchanged reload. Schema setup created no data.
The seed milestone separately verifies the reference catalog and ULA hierarchy.

The project CLI scripts use Typer and preserve the options shown above. Tests use
pytest and Typer’s CliRunner with mocked SDK calls; the default suite is offline.

Local regression tests also cover seed preview/apply options, creation order,
conflicts, partial failures, reruns, SDK value normalization, and catalog validity.
They cover rejected server checks, empty schema directories,
invalid YAML shape, and the integrity of vendored upstream files. Live negative
checks confirmed failure for missing models,
invalid credentials, and an unknown schema dependency.

```sh
uv run pytest
uv run ruff check scripts tests
uv run ruff format --check scripts tests
```

## IPAM reference seed data

Deployed to `main` after validation and merge of `ipam-seed`. All 19 prefixes and
the `global` namespace match the catalog; reruns create nothing. The native ULA
parent/child hierarchy and original default namespace designation were verified.
No individual addresses, VRFs, or allocation pools were created.

`data/ipam.yaml` defines 19 reference prefixes in a namespace named `global`.
The existing `default` namespace keeps its default designation. The name `global`
does not imply Internet routability. Descriptions include purpose and RFC links.

| Purpose | Prefixes |
| --- | --- |
| IPv4 documentation | `192.0.2.0/24`, `198.51.100.0/24`, `203.0.113.0/24` |
| IPv6 documentation | `2001:db8::/32`, `3fff::/20` |
| RFC1918 | `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16` |
| ULA aggregate / locally assigned | `fc00::/7`, `fd00::/8` |
| CGNAT shared space | `100.64.0.0/10` |
| Link-local | `169.254.0.0/16`, `fe80::/10` |
| Loopback | `127.0.0.0/8`, `::1/128` |
| Benchmarking | `198.18.0.0/15`, `2001:2::/48` |
| IPv4/IPv6 translation | `64:ff9b::/96`, `64:ff9b:1::/48` |

These are `IpamPrefix` records with `status=reserved`, no role/VRF, and
`is_pool=false`. Reserved is a catalog status, not IANA's reserved-by-protocol
classification. Aggregates contain prefixes; the single-address `::1/128` uses
`member_type=address`. Infrahub derives the `fc00::/7` → `fd00::/8` hierarchy.
For operational ULA use, generate a pseudorandom 40-bit Global ID for a `/48`
within `fd00::/8` and use `/64` subnets. This seed does not generate allocations.

After setting the lab environment variables above, preview without writes:

```sh
uv run python scripts/seed_ipam.py --branch main
```

Create an Infrahub validation branch, then apply and rerun:

```sh
uv run infrahubctl branch create ipam-seed
uv run python scripts/seed_ipam.py --branch ipam-seed --apply
uv run python scripts/seed_ipam.py --branch ipam-seed --apply
```

Use a new branch name for later changes, or resume an existing open branch.
The first run creates one namespace and 19 prefixes; an unchanged rerun skips
all 20 objects. Merge after inspecting and validating the results:

```sh
INFRAHUB_TIMEOUT=180 uv run infrahubctl branch merge ipam-seed
uv run python scripts/seed_ipam.py --branch main
```

`--data PATH` selects another catalog with the same format. The CLI validates all
input and checks all existing seed-field conflicts before writing. Differences
are reported without overwriting or deleting objects. It ignores unrelated objects
and optional non-seed relationships. It exits nonzero on conflicts or API failures.
If a write fails midway, inspect the branch and rerun: successfully created objects
are retained and matched on the next run. Creation does not use upsert.

The source catalog uses the [IANA IPv4 registry](https://www.iana.org/assignments/iana-ipv4-special-registry),
[IANA IPv6 registry](https://www.iana.org/assignments/iana-ipv6-special-registry),
and [RFC4193](https://www.rfc-editor.org/rfc/rfc4193).

## Source of truth and project boundaries

Git holds schema definitions and bootstrap configuration. Infrahub holds operational
infrastructure intent. Provisioning must be repeatable, preserve unmanaged data,
and avoid blindly overwriting operational edits.

This project configures the existing Infrahub instance. Server installation,
upgrades, and operations are outside its scope. Future Terraform/OpenTofu consumers
will use validated Infrahub data; Azure deployment code and execution belong in a
separate project.

## Roadmap

The reference catalog supplies standard special-purpose ranges. Choose real
namespace/VRF names and address allocations before adding operational data.

Later, evaluate the Schema Library's experimental Azure extension for tenants,
subscriptions, resource groups, regions, virtual networks, and subnets. Add Azure
relationships and downstream data interfaces when that work begins.

Optional future models include organization/ownership, locations, environment and
tagging conventions, and service/application ownership. Security policies,
connectivity models, and automatic IP allocation remain future candidates.

## Documentation maintenance

Update this README alongside changes to setup, commands, configuration, supported
capabilities, or milestone status. Keep working features separate from planned
work and include only implemented, verified commands. Contributor conventions
live in [AGENTS.md](AGENTS.md).

## References

- [Infrahub documentation](https://docs.infrahub.app/)
- [Schema Library](https://docs.infrahub.app/schema-library)
- [Schema Library source](https://github.com/opsmill/schema-library)
- [IPAM concepts](https://docs.infrahub.app/topics/ipam)
- [Python SDK compatibility](https://docs.infrahub.app/python-sdk/reference/compatibility)
- [infrahubctl](https://docs.infrahub.app/infrahubctl/infrahubctl)
- [Experimental Azure schema](https://docs.infrahub.app/schema-library/reference/azure)
