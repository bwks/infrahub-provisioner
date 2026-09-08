# infrahub-provisioner

## Purpose and scope

Configure an existing bare Infrahub deployment through code so that it can serve
as a source of infrastructure intent for IaC, initially focused on Azure.

The first milestone is basic IPAM: IP namespaces, prefixes, subnets, IP addresses,
and VRFs. Keep this milestone small. Automatic address allocation and relationships
to Azure resources are deferred.

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

- Use YAML for schemas and declarative bootstrap/reference data, with the official
  Python SDK and `infrahubctl` for provisioning and inspection.
- Before choosing dependencies, inspect the installed server version and consult
  the SDK compatibility matrix. Pin compatible dependencies and schema sources.
- Treat the official OpsMill Schema Library as the requested module registry.
  Inspect its metadata and schema relationships, reuse suitable models, preserve
  attribution, and record the upstream revision and local adaptations.
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

After basic IPAM works, evaluate the Schema Library's experimental Azure extension
for tenants, subscriptions, resource groups, regions, virtual networks, and subnets.
Validate and adapt it before loading it or connecting Azure objects to IPAM.
Prepare validated desired-state data for external Terraform/OpenTofu consumers;
the choice of runner and downstream interface belongs to that later milestone.

Useful optional additions, introduced only when needed:

- Organization and ownership: who owns infrastructure and addressing.
- Locations: geographic or hosting context for infrastructure.
- Environment and tagging conventions: consistent classification of resources.
- Service/application ownership: connect infrastructure to the services it supports.

Some additions may require local schema extensions. Security policies, connectivity
models, and automatic IP allocation remain future candidates, not initial scope.

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
