# Schema Library provenance

The YAML files under `schemas/` are unmodified, byte-for-byte copies from
[OpsMill/schema-library](https://github.com/opsmill/schema-library) at commit
`6116e93678c4c65d6a625163367665e11b792151`.

Copyright 2024 OpsMill SAS. The upstream Apache-2.0 license is included in
[LICENSE.txt](LICENSE.txt). This license applies to the vendored schema material.

| Local file | Upstream file |
| --- | --- |
| `schemas/dcim.yml` | `base/dcim.yml` |
| `schemas/ipam.yml` | `base/ipam.yml` |
| `schemas/location.yml` | `base/location.yml` |
| `schemas/organization.yml` | `base/organization.yml` |
| `schemas/vrf.yml` | `extensions/vrf/vrf.yml` |

The VRF extension declares `base` as a dependency. Include the complete upstream
base so device/interface, prefix scope, organization, and location references
resolve. Submit all five files together with `infrahubctl schema load schemas`;
the server resolves the combined schema dependencies. No upstream object files
or sample data are included.

Do not edit, reformat, or trim these YAML files. For an update, choose an explicit
upstream commit, copy the required files unchanged, refresh the revision and
SHA-256 values in [manifest.json](manifest.json), and validate on an Infrahub branch.
The local integrity test checks all schema files against that manifest. The paths
are flattened locally; file contents, including upstream comments, are unchanged.
