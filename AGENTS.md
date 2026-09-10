# infrahub-provisioner

## Purpose and scope

Configure an existing bare Infrahub deployment through code so that it can serve
as a source of infrastructure intent for IaC, initially focused on Azure.

The first milestone is basic IPAM: IP namespaces, prefixes, subnets, IP addresses,
and VRFs. Keep this milestone small. Automatic address allocation and relationships
to Azure resources are deferred.

The schemas-only milestone is complete. The seed workflow manages the agreed
19 special-purpose prefixes in the existing `default` namespace from `data/ipam.yaml`.
Do not invent operational allocations or modify upstream schemas.

This repository owns Infrahub schemas, bootstrap/reference data, provisioning
automation, and eventually validated data interfaces for Terraform/OpenTofu
consumers. Azure deployment code and execution belong in a separate project.
Infrahub server installation, upgrades, and operations are outside this project.

## Current state

At project initialization on 2026-09-08:

- The repository contained no implementation or tooling.
- `http://ihub01:8000` responded successfully.
- Username/password login and authenticated GraphQL access were verified.
- The live REST OpenAPI document reported version `1.10.6`.
- One account token named `Created automatically` existed; its value was not
  retrieved. No dedicated automation token was created.

These are observations, not permanent assumptions. Inspect the current server,
schema, and repository before making changes. Update this guidance as working
commands and implementation conventions become established.

The first schema implementation now uses `uv`, SDK `1.22.1`, unmodified upstream
YAML at the root of `schemas/`, local extensions under `schemas/local/`,
and the built-in `IpamNamespace`. See [README.md](README.md) for current
deployment status and verified commands, and
[schema provenance](third_party/schema-library/README.md) for the upstream pin and
required base dependencies.

## Lab access and API

This is a lab. The user explicitly authorized documenting these default credentials:

```sh
export INFRAHUB_ADDRESS='http://ihub01:8000'
export INFRAHUB_USERNAME='admin'
export INFRAHUB_PASSWORD='infrahub'
```

The official SDK supports username/password authentication. Direct REST login uses
`POST /api/auth/login` with JSON fields `username` and `password`; the returned
temporary access token authenticates requests using `Authorization: Bearer <token>`.

For automation, use `INFRAHUB_API_TOKEN`. Direct API requests using an API token
send `X-INFRAHUB-KEY: <token>`. The user has authorized creating a dedicated
automation token when needed; do not ask for that permission again. Existing
API token values cannot be retrieved after creation. Store generated tokens outside
tracked files and avoid printing them in logs. The credential exception above
applies specifically to the supplied lab defaults.

Useful endpoints relative to `INFRAHUB_ADDRESS`:

| Endpoint | Purpose |
| --- | --- |
| `/graphql` | GraphQL access to the main branch |
| `/graphql/<branch>` | GraphQL access to a specific branch |
| `/schema.graphql` | Download the generated GraphQL schema |
| `/schema.graphql?branch=<branch>` | Download a branch's GraphQL schema |
| `/api/openapi.json` | Discover the live REST API and request/response schemas |

GraphQL is the primary data interface and reflects the loaded Infrahub schema.
Inspect that schema rather than assuming node names or mutation shapes. Select
the target Infrahub branch explicitly for provisioning and validation.

## Implementation conventions

- Include `Co-authored-by: Codex <noreply@openai.com>` as a Git commit trailer
  on commits containing Codex contributions, as requested by the user. Preserve
  the user's primary authorship.
- Use pytest for tests and Typer for project CLI tools. Test CLI behavior with
  Typer’s CliRunner and mock SDK calls so the default test suite stays offline.

- Use YAML for schemas and declarative bootstrap/reference data, with the official
  Python SDK and `infrahubctl` for provisioning and inspection.
- Before choosing dependencies, inspect the installed server version and consult
  the SDK compatibility matrix. Pin compatible dependencies and schema sources.
- Treat the official OpsMill Schema Library as the requested module registry.
  Inspect its metadata and schema relationships, reuse suitable models, preserve
  attribution, and record the upstream revision. Use upstream schemas unchanged;
  do not trim, reformat, or customize vendored YAML. Include required dependencies.
- Load required base schemas and extension dependencies in order. Avoid unrelated
  optional extensions. The library contains experimental examples, so validate
  compatibility instead of assuming every schema is production-ready or compatible
  with every other extension.
- Keep Git authoritative for schema definitions and bootstrap configuration.
  Infrahub holds operational infrastructure intent. Define which data provisioning
  manages, and do not blindly overwrite operational edits during bootstrap reruns.
- Make provisioning repeatable using stable identifiers, dependency-aware loading,
  and clear errors. Repeated runs must not create duplicates or delete unmanaged data.
- Validate schema changes on a dedicated Infrahub branch before integration.
- Document actual setup, provisioning, and validation commands when implemented.
  Do not present planned commands as existing tools.
- Use `uv sync --locked` for setup. Run `scripts/check_schema.py --branch <branch>`
  through `uv run python` for schema validation: the pinned upstream CLI check can
  return zero on server rejection. Load with `infrahubctl schema load schemas` and
  an explicit `--branch`, then run `scripts/verify_schema.py` against that branch.
- Run `uv run pytest` for check-command regression
  tests and `uv run ruff check scripts tests` plus `uv run ruff format --check scripts
  tests` for Python changes. Schema verification reads schema and counts only; hierarchy validation also reads
  Azure tenant/group/subscription fields without writes;
  keep test fixtures offline. Seed only the catalog data explicitly requested by
  the user during live integration checks.
- Update [README.md](README.md) in the same change whenever setup, commands,
  configuration, supported capabilities, or milestone status changes. Keep current
  functionality distinct from planned work, and document only verified commands.

## Initial IPAM model

- Build on Infrahub's IPAM generics: `BuiltinIPNamespace`, `BuiltinIPPrefix`, and
  `BuiltinIPAddress`, and reuse compatible Schema Library definitions.
- Represent subnets as prefixes. Use native prefix/address hierarchy and namespace
  isolation rather than duplicating those mechanisms in custom attributes.
- Distinguish an IP namespace, which isolates addressing, from a VRF, which models
  a routing context. Inspect available VRF models before introducing a custom one;
  do not assume every namespace is a VRF or enforce a one-to-one mapping by default.
- Support overlapping address space across isolated namespaces. Preserve the
  native ability to represent both IPv4 and IPv6.
- Do not add Azure relationships or allocation pools to the initial milestone.
- Obtain real address ranges, namespace/VRF names, and reference data when data
  provisioning becomes the task. Do not invent operational values.

## Later milestones and suggested models

The experimental Azure extension is now included unchanged for tenants,
subscriptions, resource groups, regions, virtual networks, and subnets.
The shared verifier requires IPAM and Azure schemas, checks their relationships,
and queries Azure object counts. This schema step adds no Azure resource data.
Prepare validated desired-state data for external Terraform/OpenTofu consumers;
the choice of runner and downstream interface belongs to that later milestone.

Useful optional additions, introduced only when needed:

- Organization and ownership: who owns infrastructure and addressing.
- Locations: geographic or hosting context for infrastructure.
- Environment and tagging conventions: consistent classification of resources.
- Service/application ownership: connect infrastructure to the services it supports.

Use unmodified upstream modules for later additions as well. The required base
already supplies organization/location foundations; further optional extensions
remain deferred. Broader security policies, connectivity models, and automatic IP
allocation remain future candidates, not initial scope.

## Validation expectations

For documentation-only changes, review Markdown, links, and consistency with the
agreed scope; no application tests are necessary.

As provisioning is implemented, validate these meaningful scenarios:

- A bare compatible instance accepts the selected schemas and bootstrap data.
- An unchanged rerun produces no duplicates and preserves unmanaged data and
  operational edits.
- Prefixes, subnets, and addresses form the expected native hierarchy.
- Identical address ranges remain isolated in different namespaces.
- VRF representation preserves the distinction between routing contexts and
  addressing namespaces.
- Invalid input, missing dependencies, and authentication failures produce clear
  errors without being reported as successful provisioning.

Report what was changed and verified, along with any unresolved compatibility or
access limitations. Distinguish local checks from checks run against Infrahub.

## References

- [Infrahub documentation](https://docs.infrahub.app/)
- [Schema Library](https://docs.infrahub.app/schema-library)
- [Schema Library source](https://github.com/opsmill/schema-library)
- [IPAM concepts](https://docs.infrahub.app/topics/ipam)
- [Library IPAM schema](https://docs.infrahub.app/schema-library/reference/ipam)
- [Experimental Azure schema](https://docs.infrahub.app/schema-library/reference/azure)
- [GraphQL API](https://docs.infrahub.app/development-resources/graphql/overview)
- [Managing API tokens](https://docs.infrahub.app/deploy-manage/user-management/managing-api-tokens)
- [SDK client and authentication](https://docs.infrahub.app/python-sdk/guides/client)
- [SDK configuration](https://docs.infrahub.app/python-sdk/reference/config)
- [SDK compatibility matrix](https://docs.infrahub.app/python-sdk/reference/compatibility)
- [infrahubctl](https://docs.infrahub.app/infrahubctl/infrahubctl)

## IPAM seed workflow

The catalog was moved from global to default on branch `ipam-default` and merged
into Infrahub `main`. All 19 prefix IDs were preserved; global was deleted only
after it was empty. The original default namespace and its designation remain.

- Use `uv run python scripts/seed_ipam.py --branch <branch>` for read-only preview;
  add `--apply` to create missing catalog objects. The optional `--data` selects YAML.
- Preflight all seed-field conflicts before writes. Never overwrite existing edits
  or delete objects. Partial failures are not rolled back; inspect and rerun.
- Identify prefixes by namespace plus canonical CIDR. Preserve the existing
  `default` namespace and its default flag. The catalog now selects `default` by
  name only; its description/default metadata are unmanaged by seed reruns.
- Name-only namespace selectors must already exist. A catalog with a namespace
  description may create a missing non-default namespace and checks its metadata
  on reruns. Never recreate or change the built-in default designation.
- `scripts/move_ipam_to_default.py --branch <branch>` previews the one-time catalog
  move from global to default; `--apply` moves prefixes while preserving IDs, checks
  native hierarchy, and deletes global only after generic prefix/address counts
  are zero. The normal seed still never moves or deletes data. Run migration on
  an isolated branch with one writer; inspect and rerun after partial failures.
- Catalog status `reserved` is not IANA reserved-by-protocol metadata. No allocation
  pools, VRFs, individual address objects, or schema modifications are seeded.


## Local Azure management-group extension

- Keep vendored YAML unchanged. Repository-owned extensions belong under
  `schemas/local/`; both the checker and loader discover schema YAML recursively.
  Hash verification remains mandatory for every file in the upstream manifest.
- `AzureManagementGroup` uses `AzureManagementGroupHierarchy` for native hierarchy,
  requires a tenant, and scopes management-group ID uniqueness to that tenant.
  Use Infrahub IDs or tenant plus group ID; no HFID or stored root flag is defined.
  Groups do not inherit the resource-group-scoped `AzureResource` generic.
- Local tenant/subscription extensions preserve the upstream subscription tenant
  relationship. Subscription group membership is optional for editing.
- Run `uv run python scripts/check_azure_hierarchy.py --branch <branch>` as the
  read-only complete-hierarchy gate. It uses SDK pagination and requires one root
  matching each tenant ID when both IDs are populated, same-tenant acyclic parent
  paths, at most six levels
  beneath the root, and a same-tenant group for every subscription. It rejects
  case-insensitive duplicate identifiers and invalid Microsoft group identifiers.
- Exit `0` means valid (including explicitly empty inventory); `1` means invalid
  data or read failure. These checks do not automatically enforce UI/API writes.
- Tenant/subscription Azure GUIDs and the root group ID are optional for planned
  resources; populated values must be GUIDs. Non-root group IDs are required by the
  CLI. Unknown GUIDs are reported as pending and ignored in duplicate checks.
- Validate hierarchy edge cases offline; live seeding is limited to the explicitly
  requested fake-corp catalog. Synchronization, policy/RBAC, and execution remain deferred.

## Azure lifecycle status

- `schemas/local/azure_status.yml` supplies status dropdowns on the original seven
  Azure node types, plus the resource and management-group hierarchy generics.
  Network policy reuses these choices for NSGs, route tables, rules, and routes.
  Options are Planned, Active, Reserved, Deprecated, and Unmanaged. Regions
  (`AzureRegion`) default to Unmanaged because they are Azure-managed reference
  configuration; other Azure types default to Planned.
- These are schema attribute choices, not a global status registry. Upstream IPAM
  status definitions remain unchanged. Preserve the hierarchy generic's metadata
  in its local status extension, including `hierarchical: true`.
- Status is operational intent: seed reruns must preserve user edits. No Azure
  execution, discovery, or automatic status transitions are implemented.

## Planned Azure hierarchy seed

The fake-corp catalog is deployed to Infrahub `main` after validation and merge of
`fake-corp-hierarchy`, extended by `azure-subscriptions`: one tenant, 13 groups,
12 subscriptions, and 14 pending Azure GUIDs. The original IPAM catalog is unchanged.

- Use `uv run python scripts/seed_azure_hierarchy.py --branch <branch>` for read-only
  preview; add `--apply` to create missing catalog objects. `--data` selects YAML.
- `data/azure_hierarchy.yaml` owns the fake-corp tenant name, root display name, and
  12 non-root group IDs, display names, and parents. IDs have no organization prefix.
  Together with the tenant root there are 13 management groups. The optional
  `subscriptions` list contains name, management_group ID, and initial status.
- Match tenants by stable catalog name, groups by tenant plus case-insensitive group
  ID, and the root by tenant plus no parent. Reject ambiguous matches. Tenant-name
  changes select a different tenant; the seed does not implement renames.
- Preflight all managed-field conflicts and validate the entire projected hierarchy
  before writes. Create in dependency order; never overwrite edits, move groups, or
  delete unmanaged objects. Use one seed writer per branch. Partial writes require
  inspection and rerun; no rollback is attempted.
- Azure-assigned GUIDs are operational data and are omitted from the seed catalog.
  Preserve GUIDs, descriptions, and other unmanaged fields populated in Infrahub.
  Tenant/root GUID assignment is a later manual or synchronization step, not a seed
  action. Schema constraints and CLI hierarchy validation remain separate gates.


## Cloud location catalog

- `schemas/local/cloud_locations.yml` replaces empty `AzureLocation` with
  `AzureRegion` and retargets `location` relationships (UI label Region). Check the
  old type is empty before schema removal on any deployment; stop if data exists.
  Keep all upstream YAML unchanged. `/objects/AzureRegion` replaces the old route.
- `LocationGroup` and `AzureRegion` inherit `LocationGeneric`. Seed Cloud, Azure,
  AWS, six Azure geographic groups, and 57 public-cloud regions from
  `data/cloud_locations.yaml`. AWS remains empty. Internal group names are globally
  unique; display names are readable. Region names retain Azure programmatic IDs.
- `uv run python scripts/seed_cloud_locations.py --branch <branch>` previews;
  `--apply` creates missing records, `--data` selects a catalog. Preserve operational
  status, tags, and descriptions. Preflight conflicts before all writes, use one
  writer, never move/delete existing objects, and inspect/rerun partial failures.
- Geographic parent mappings are explicit catalog data, not inferred from names.
  Include restricted public regions, exclude future/sovereign regions, and refresh
  the dated Microsoft reference snapshot deliberately. No live Azure discovery,
  AWS regions, country groups, availability zones, or deployment execution.


## Landing-zone subscriptions

- Seed the 12 diagram subscriptions through the existing Azure hierarchy command.
  Use readable names without the word Subscription or an organization prefix.
  Corp holds A1, A2, and P1; Local holds LC1 and LA1; Sandbox holds 1 and 2;
  the four Platform child groups and Decommissioned each hold one. Online is empty.
- Match subscription names case-insensitively within their tenant. Reject ambiguous
  matches and conflicting names/membership before any writes. Create subscriptions
  after groups; include them in projected complete-hierarchy validation.
- Status is creation-only: Decommissioned starts Deprecated, the other 11 Planned.
  Leave subscription GUIDs unset and preserve later GUID/status edits. Catalogs
  without subscriptions remain supported. No schema changes or Azure execution.


## Azure key/value tags

- `schemas/local/resource_tags.yml` adds AzureTaggable inheritance to subscriptions,
  resource groups, and VNets. Keep it after the other local Azure extensions in
  schema discovery order; verify all owner types on the live composed schema.
  Network policy adds NSG and route-table owners through the same generic.
- AzureTag is an independently owned key/value assignment, with one owner and no
  shared-value propagation. Existing BuiltinTag labels remain separate and intact.
  Do not add Azure tags to tenants, management groups, regions, or subnets.
- `uv run python scripts/check_azure_tags.py --branch <branch>` validates ownership,
  case-insensitive keys, Azure character/length limits, and 50 tags per owner.
  It is read-only, paginated, returns 0 for valid/empty and 1 for invalid/read failure.
  Schema exact-key constraints and the CLI case-insensitive gate are distinct.
- No live tag values are seeded. Preserve operational assignments on all seed
  reruns. Policy inheritance, Azure synchronization, and execution remain deferred.


## Resource-group seed

- `data/azure_resource_groups.yaml` owns the requested `rg-conn-prd-network` in
  fake-corp / Connectivity, located in Australia East, initially Planned, for
  network resources. It creates no tags, dependent objects, or actual Azure resources.
- `uv run python scripts/seed_azure_resource_groups.py --branch <branch>` previews;
  add `--apply` to create missing groups, or `--data` for a custom catalog.
- Resolve existing tenants, subscriptions, and regions uniquely. Match group names
  case-insensitively within the selected subscription. Preflight all conflicts;
  never overwrite names/regions, move groups, or delete objects. Preserve operational
  status and tags on reruns. After partial failures, inspect and rerun.


## Resource-group uniqueness

- `AzureResourceGroup` uses `[subscription, name_key__value]` uniqueness. The
  required read-only Jinja2 `name_key` computes lowercase from `name`; preserve
  displayed names and exclude region from identity. Do not make names globally unique.
- For preexisting groups without name_key, use the staged migration documented in
  README: optional computed field, equivalent template update to trigger backfill,
  verify all values/no scoped duplicates, then required field. Final committed
  schema is required; do not bypass migration checks or manually maintain the key.


## Virtual-network foundation

- `schemas/local/virtual_networks.yml` extends AzureVirtualNetwork only. Preserve
  AzureResource and AzureTaggable inheritance, existing API names, and subnet links.
- Require resourcegroup, location (Region), and address_space with min_count 1,
  including Planned records. Region is independent of the resource group's region;
  subscription is reached through resourcegroup. Keep BuiltinIPPrefix relationships
  for IPv4/IPv6 and namespace isolation, without new CIDR fields or seed data.
- Enforce Azure VNet naming rules and resource-group-scoped case-insensitive
  uniqueness through a required read-only computed name_key. Use attribute
  parameters for length/regex constraints; the pinned SDK exposes legacy fields
  when reading the server schema.
- Before loading on another deployment, inspect existing VNets for required-field
  and uniqueness compliance. Migrate populated deployments deliberately; never
  invent address space or bypass required relationships to load a schema.
- Schema constraints do not validate address overlap, subnet containment, or Azure
  deployment readiness. DNS, further subnet configuration, identifiers, allocation,
  seeding, and deployment remain deferred. Keep scenario fixtures offline.


## Connectivity hub VNet seed

- `data/azure_hub_vnet.yaml` owns the agreed vnet-conn-prd-hub in fake-corp /
  Connectivity / rg-conn-prd-network, Australia East, with 10.150.0.0/24 in default.
  The VNet starts Planned and its new prefix Reserved. No tags or subnets are seeded.
- `uv run python scripts/seed_azure_virtual_network.py --branch <branch>` previews
  one VNet; `--apply` creates missing prefixes then the VNet, and `--data` selects YAML.
- Require existing unique dependencies. Match prefixes by namespace/canonical CIDR
  and VNets by resource-group/case-insensitive name. Preflight all conflicts before
  writes, including same-namespace overlap with other modeled VNets; allow native
  parent containers such as the catalog's 10/8. This is not a general Azure validator.
- Preserve status, tags, and prefix descriptions on reruns. Never move/delete or
  overwrite existing records. Inspect partial failures and rerun with one writer.
- The 19 reference prefixes remain in data/ipam.yaml; the operational hub prefix
  is owned by the VNet catalog. Verify both previews and native prefix parentage.


## Subnets and network policy

- `schemas/local/network_policy.yml` strengthens AzureVirtualNetworkSubnet and adds
  AzureNetworkSecurityGroup, AzureNetworkSecurityRule, AzureRouteTable, and AzureRoute.
  NSGs/route tables inherit AzureResource and AzureTaggable; children have their own
  Planned lifecycle status but no duplicated region/resource-group or Azure tags.
- Subnets require a VNet and at least one BuiltinIPPrefix, with optional single NSG
  and route-table associations. Shared associations across resource groups are valid
  when VNet subscription and region match. Preserve API names and native IPAM.
- Scope computed lowercase name uniqueness to the owning parent. Custom rule
  priorities are 100–4096 and unique per NSG/direction; no default rules are modeled.
- `uv run python scripts/check_azure_networks.py --branch <branch>` is a read-only
  gate with top-level and nested-prefix pagination. It checks subnet containment,
  namespace and overlap, association context, required references, scoped identity,
  rule expressions, and route next-hop combinations. Exit 0 valid/empty; 1 invalid
  or read failure. Do not claim CLI-only checks enforce UI/API writes.
- Expression Text fields accept documented comma-separated IPs/CIDRs and ports/ranges,
  a single service tag, or wildcard where applicable. Validate service-tag syntax
  only; no live catalog lookup. Routing/security expressions are not IPAM allocations.
- BGP route propagation is enabled by default. Next-hop IP is required only for
  Virtual Appliance. No ECMP, ASGs, default rules/system routes, effective-policy
  calculation, or Azure deployment. Keep fixtures offline and seed nothing until
  actual subnet/policy intent is explicitly requested.


## Hub subnets and delegation

- `data/azure_hub_subnets.yaml` owns the six agreed subnet names/ranges under the
  existing hub /24. Two dns-resolver delegation records target
  Microsoft.Network/dnsResolvers; the four other subnets remain undelegated.
- `schemas/local/subnet_delegation.yml` supplies named AzureSubnetDelegation children
  with service_name, parent subnet, Planned status, and computed case-insensitive
  subnet-scoped name/service uniqueness. No duplicated region, group, GUID, or tags.
- `uv run python scripts/seed_azure_subnets.py --branch <branch>` previews;
  `--apply` creates missing prefixes, subnets, then delegations; `--data` selects YAML.
  Validate the full projected network inventory before writes. Preserve operational
  status, description, policy associations, and unmanaged records. Extra or changed
  delegations conflict; missing desired assignments can be added. Never overwrite
  or delete objects. Inspect partial writes and rerun with one writer.
- Network validation adds service syntax/ownership/uniqueness and DNS-specific
  exclusive delegation with one IPv4 /24–/28 prefix. Service availability and other
  service-specific constraints remain undiscovered; these semantic checks are CLI
  gates rather than automatic UI/API enforcement.
- Keep the 19-prefix reference catalog and hub allocation catalog unchanged.
  New subnet prefixes are Reserved; subnets/delegations start Planned. Seed no
  NSGs, routes, private-endpoint policies, endpoints, or deployed Azure services.

## Subnet service endpoints

- `schemas/local/subnet_service_endpoints.yml` adds owned AzureSubnetServiceEndpoint
  selections through subnet `service_endpoints`. The required service_name dropdown
  contains ten classic Azure service identifiers verified on 2026-09-10; selections
  default Planned, with no tags or separate name/GUID.
- Required computed service_key lowercases the identifier and normalizes
  Microsoft.Storage.Global to Microsoft.Storage. Schema uniqueness on subnet plus
  service_key prevents duplicate selections and the mutually exclusive Storage
  modes. Keep this normalization aligned with check_azure_networks.py.
- The network CLI validates choices, parent references, and conflicts. Subnet seed
  reruns preserve operational selections; no endpoint selections are in seed data.
- Location restrictions, endpoint policies, network identifiers, service availability,
  target-service firewall configuration, and Azure execution remain outside scope.

## Paired VNet peering

- `schemas/local/virtual_network_peering.yml` defines AzureVirtualNetworkPeering:
  one intent connection with required virtual_network_a/b, peering_name_a/b and
  four required Boolean settings per end, plus optional description and shared
  Planned status. VNet access defaults true; other settings default false.
- VNet peerings_a/b reverse relationships reference the two end positions. Neither
  end is a Parent/Component owner. No AzureResource/AzureTaggable inheritance, HFID,
  or GUID requirement. Consumers must map each record into two Azure resources.
- Schema enforces ordered-pair uniqueness and name syntax. Network CLI checks
  unordered pairs, self-peering, required references, names scoped to the local
  VNet across both end positions, and address-space overlap across IPAM namespaces.
- Remote gateway use requires opposite-end gateway transit; reject both ends using
  remote gateways and a VNet selecting multiple remote gateways across connections.
  Apply CLI checks to all statuses. Do not assume traffic settings are symmetric.
- Allow cross-region/subscription/tenant connections. Actual gateways, permissions,
  cloud compatibility, deployment readiness, subnet peering, and synchronization
  are outside scope. Seed no peerings or new VNets without explicit user intent.

Infrahub reports the peering's defaulted Boolean attributes as optional after
schema normalization, despite `optional: false` in YAML. The verifier accepts
this server representation while checking exact defaults. The network gate
requires an actual Boolean value for all eight settings and rejects null values.

- Peering rollout recovered on 2026-09-10 after explicit user authorization. Both
  merge tasks were CRASHED while the branch remained MERGING. After snapshotting
  and comparing all 130 modeled objects (IDs, attributes, relationships) with main,
  the failed branch was deleted through the API. Main then had an empty schema
  diff and unchanged reload; verification and seed previews passed, no MERGING
  branches remained, and active-worker schema hashes agreed. No database edits or
  server restarts were performed. Do not generalize this into deleting merging
  branches without confirming terminal task state and preserving branch data.
