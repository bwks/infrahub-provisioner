"""Read-only verification of deployed IPAM and Azure schemas; creates no objects."""

import sys
from typing import Annotated

import typer
from infrahub_sdk import InfrahubClientSync

app = typer.Typer(help=__doc__, add_completion=False, pretty_exceptions_enable=False)

AZURE_NODES = (
    "AzureManagementGroup",
    "AzureRegion",
    "AzureTenant",
    "AzureSubscription",
    "AzureResourceGroup",
    "AzureVirtualNetwork",
    "AzureVirtualNetworkSubnet",
    "AzureNetworkSecurityGroup",
    "AzureNetworkSecurityRule",
    "AzureRouteTable",
    "AzureRoute",
    "AzureSubnetDelegation",
    "AzureSubnetServiceEndpoint",
    "AzureVirtualNetworkPeering",
    "AzureStorageAccount",
    "AzureBlobContainer",
    "AzureTerraformStateBackend",
    "AzurePrivateDnsZone",
    "AzurePrivateDnsZoneLink",
    "AzurePrivateDnsRecordSet",
    "AzureDnsPrivateResolver",
    "AzureDnsInboundEndpoint",
    "AzureDnsOutboundEndpoint",
    "AzureDnsForwardingRuleset",
    "AzureDnsForwardingRule",
    "AzureDnsForwardingRulesetLink",
)


AZURE_STATUSES = {"planned", "active", "reserved", "deprecated", "unmanaged"}


def verify_azure(schemas) -> None:
    for kind in (*AZURE_NODES, "AzureResource", "AzureManagementGroupHierarchy"):
        if kind not in schemas:
            raise ValueError(f"{kind} is missing")
    for kind in (*AZURE_NODES, "AzureResource", "AzureManagementGroupHierarchy"):
        status = schemas[kind].get_attribute_or_none("status")
        default = "unmanaged" if kind == "AzureRegion" else "planned"
        if (
            status is None
            or status.kind != "Dropdown"
            or not status.optional
            or status.default_value != default
            or {choice["name"] for choice in status.choices or []} != AZURE_STATUSES
        ):
            raise ValueError(
                f"{kind}.status must be an Azure lifecycle dropdown defaulting to {default}"
            )
    if "AzureLocation" in schemas:
        raise ValueError("AzureLocation must be retired in favor of AzureRegion")
    for kind in ("AzureRegion", "LocationGroup"):
        if kind not in schemas:
            raise ValueError(f"{kind} is missing")
        node = schemas[kind]
        if (
            node.hierarchy != "LocationGeneric"
            or "LocationGeneric" not in node.inherit_from
        ):
            raise ValueError(f"{kind} must use the LocationGeneric hierarchy")
        if node.display_label != "display_name__value":
            raise ValueError(f"{kind} must display its readable name")
        if not node.get_attribute("name").unique:
            raise ValueError(f"{kind}.name must be unique")
        if node.get_attribute("display_name").optional:
            raise ValueError(f"{kind}.display_name must be required")
    for kind in ("AzureResource", "AzureResourceGroup", "AzureVirtualNetwork"):
        if schemas[kind].get_relationship("location").label != "Region":
            raise ValueError(f"{kind}.location must be labeled Region")
    resource_group = schemas["AzureResourceGroup"]
    if not any(
        set(c) == {"subscription", "name_key__value"}
        for c in resource_group.uniqueness_constraints or []
    ):
        raise ValueError(
            "AzureResourceGroup uniqueness must use subscription and normalized name"
        )
    normalized = resource_group.get_attribute_or_none("name_key")
    if (
        normalized is None
        or normalized.kind != "Text"
        or not normalized.read_only
        or normalized.optional
        or normalized.unique
    ):
        raise ValueError(
            "AzureResourceGroup.name_key must be required read-only Text, scoped rather than globally unique"
        )
    if "AzureResource" not in schemas["AzureVirtualNetwork"].inherit_from:
        raise ValueError("AzureVirtualNetwork must inherit from AzureResource")
    vnet = schemas["AzureVirtualNetwork"]
    if vnet.uniqueness_constraints != [["resourcegroup", "name_key__value"]]:
        raise ValueError(
            "AzureVirtualNetwork uniqueness must use resourcegroup and normalized name"
        )
    key = vnet.get_attribute_or_none("name_key")
    if (
        key is None
        or key.kind != "Text"
        or key.optional
        or not key.read_only
        or key.unique
    ):
        raise ValueError(
            "AzureVirtualNetwork.name_key must be required read-only scoped Text"
        )
    name = vnet.get_attribute("name")
    if (
        name.optional
        or name.unique
        or name.min_length != 2
        or name.max_length != 64
        or name.regex != r"^[A-Za-z0-9][A-Za-z0-9_.-]*[A-Za-z0-9_]\Z"
    ):
        raise ValueError("AzureVirtualNetwork.name must enforce Azure naming rules")
    for field in ("resourcegroup", "location", "address_space"):
        relationship = vnet.get_relationship(field)
        if relationship.optional or (
            field == "address_space" and relationship.min_count != 1
        ):
            raise ValueError(f"AzureVirtualNetwork.{field} must be required")
    verify_dns(schemas)
    verify_storage(schemas)
    verify_peering(schemas)
    verify_service_endpoints(schemas)
    verify_subnet_delegation(schemas)
    verify_network_policy(schemas)
    group = schemas["AzureManagementGroup"]
    if (
        group.hierarchy != "AzureManagementGroupHierarchy"
        or "AzureManagementGroupHierarchy" not in group.inherit_from
    ):
        raise ValueError("AzureManagementGroup must use its native hierarchy generic")
    if "AzureResource" in group.inherit_from:
        raise ValueError("AzureManagementGroup must not inherit AzureResource")
    if not any(
        set(c) == {"tenant", "management_group_id__value"}
        for c in group.uniqueness_constraints
    ):
        raise ValueError("AzureManagementGroup uniqueness must be scoped to tenant")
    for name in ("management_group_id", "display_name", "description"):
        attribute = group.get_attribute(name)
        if attribute.kind != "Text" or attribute.optional != (name != "display_name"):
            raise ValueError(f"AzureManagementGroup.{name} has an invalid definition")
    for kind, name in (
        ("AzureTenant", "tenant_id"),
        ("AzureSubscription", "subscription_id"),
    ):
        attribute = schemas[kind].get_attribute(name)
        if attribute.kind != "Text" or not attribute.optional:
            raise ValueError(f"{kind}.{name} must be optional Text")
    for kind, name, optional in (
        ("AzureManagementGroup", "tenant", False),
        ("AzureSubscription", "management_group", True),
    ):
        relationship = schemas[kind].get_relationship_or_none(name)
        if relationship is None or relationship.optional != optional:
            raise ValueError(f"{kind}.{name} optional must be {optional}")
    relationships = (
        ("AzureTenant", "management_groups", "AzureManagementGroup", "many"),
        ("AzureManagementGroup", "tenant", "AzureTenant", "one"),
        ("AzureManagementGroup", "subscriptions", "AzureSubscription", "many"),
        ("AzureManagementGroup", "parent", "AzureManagementGroupHierarchy", "one"),
        ("AzureManagementGroup", "children", "AzureManagementGroupHierarchy", "many"),
        ("AzureSubscription", "management_group", "AzureManagementGroup", "one"),
        ("AzureTenant", "subscriptions", "AzureSubscription", "many"),
        ("AzureSubscription", "tenant", "AzureTenant", "one"),
        ("AzureSubscription", "resourcegroups", "AzureResourceGroup", "many"),
        ("AzureResourceGroup", "subscription", "AzureSubscription", "one"),
        ("AzureResourceGroup", "location", "AzureRegion", "one"),
        ("AzureResource", "location", "AzureRegion", "one"),
        ("AzureResource", "resourcegroup", "AzureResourceGroup", "one"),
        ("AzureVirtualNetwork", "location", "AzureRegion", "one"),
        ("AzureVirtualNetwork", "resourcegroup", "AzureResourceGroup", "one"),
        ("AzureVirtualNetwork", "address_space", "BuiltinIPPrefix", "many"),
        ("AzureVirtualNetwork", "subnets", "AzureVirtualNetworkSubnet", "many"),
        ("AzureVirtualNetworkSubnet", "virtualnetwork", "AzureVirtualNetwork", "one"),
        ("AzureVirtualNetworkSubnet", "address_prefixes", "BuiltinIPPrefix", "many"),
    )
    for kind, name, peer, cardinality in relationships:
        relationship = schemas[kind].get_relationship_or_none(name)
        if (
            relationship is None
            or relationship.peer != peer
            or relationship.cardinality != cardinality
        ):
            raise ValueError(f"{kind}.{name} must refer to {cardinality} {peer}")


def verify_dns(schemas):
    if __package__:
        from .check_azure_dns import (
            DNS_KINDS,
            RESOLVER_NAME_RE,
            TAGGABLE,
            REFERENCES,
            SCOPES,
            ZONE,
            ZONE_LINK,
            RECORD,
            RESOLVER,
            INBOUND,
            OUTBOUND,
            RULESET,
            RULE,
            RULESET_LINK,
        )
    else:
        from check_azure_dns import (
            DNS_KINDS,
            RESOLVER_NAME_RE,
            TAGGABLE,
            REFERENCES,
            SCOPES,
            ZONE,
            ZONE_LINK,
            RECORD,
            RESOLVER,
            INBOUND,
            OUTBOUND,
            RULESET,
            RULE,
            RULESET_LINK,
        )
    if __package__:
        from .dns_zone_catalog import choices
    else:
        from dns_zone_catalog import choices
    zone = schemas[ZONE]
    selection = zone.get_attribute("zone_selection")
    if (
        selection.kind != "Dropdown"
        or selection.optional
        or {c["name"] for c in selection.choices or []} != {"custom", *choices()}
    ):
        raise ValueError(
            "Private DNS zone selection must expose the pinned Azure catalog and Custom"
        )
    if (
        not zone.get_attribute("name").read_only
        or not zone.get_attribute("custom_name").optional
    ):
        raise ValueError(
            "Private DNS zone names must be computed from selection or custom input"
        )
    for kind in DNS_KINDS:
        node = schemas[kind]
        expected = (
            {"AzureTaggable"}
            if kind == ZONE
            else {"AzureResource", "AzureTaggable"}
            if kind in {RESOLVER, RULESET}
            else set()
        )
        if set(node.inherit_from) != expected:
            raise ValueError(f"{kind} has invalid DNS inheritance")
        if kind == ZONE and node.get_relationship_or_none("location"):
            raise ValueError("Private DNS zones must be global without a region")
        name, key = node.get_attribute("name"), node.get_attribute("name_key")
        if (
            name.kind != "Text"
            or name.optional
            or name.unique
            or name.min_length != 1
            or name.max_length != (253 if kind in {ZONE, RECORD} else 80)
        ):
            raise ValueError(f"{kind}.name has invalid DNS constraints")
        if (
            kind in {RESOLVER, INBOUND, OUTBOUND, RULESET}
            and name.regex != RESOLVER_NAME_RE
        ):
            raise ValueError(f"{kind}.name must enforce resolver naming rules")
        if key.kind != "Text" or key.optional or not key.read_only or key.unique:
            raise ValueError(f"{kind}.name_key must be required read-only scoped Text")
        uniqueness = [[SCOPES[kind], "name_key__value"]]
        if kind == RECORD:
            uniqueness[0].append("record_type__value")
        if kind in {ZONE_LINK, RULESET_LINK}:
            uniqueness.append([SCOPES[kind], "virtual_network"])
        if kind == RESOLVER:
            uniqueness.append(["virtual_network"])
        if kind in {INBOUND, OUTBOUND}:
            uniqueness.append(["subnet"])
        if kind == RULE:
            uniqueness.append(["ruleset", "domain_key__value"])
        if node.uniqueness_constraints != uniqueness:
            raise ValueError(f"{kind} has invalid DNS uniqueness")
        for field, peer in REFERENCES[kind].items():
            r = node.get_relationship(field)
            relkind = "Parent" if field == SCOPES[kind] else "Attribute"
            if (r.peer, r.cardinality, r.optional, r.kind) != (
                peer,
                "one",
                False,
                relkind,
            ):
                raise ValueError(f"{kind}.{field} has invalid DNS relationship")
    for kind, field in [(RECORD, "records"), (RULE, "target_servers")]:
        a = schemas[kind].get_attribute(field)
        if a.kind != "JSON" or a.optional:
            raise ValueError(f"{kind}.{field} must be required JSON")
    for kind, field, choices, default in [
        (RECORD, "record_type", {"A", "AAAA", "CNAME", "TXT"}, None),
        (INBOUND, "allocation_method", {"Static", "Dynamic"}, "Dynamic"),
    ]:
        a = schemas[kind].get_attribute(field)
        if (
            a.kind != "Dropdown"
            or {c["name"] for c in a.choices or []} != choices
            or a.default_value != default
        ):
            raise ValueError(f"{kind}.{field} has invalid DNS choices/default")
    for kind, field, default in [
        (ZONE_LINK, "registration_enabled", False),
        (RULE, "enabled", True),
    ]:
        a = schemas[kind].get_attribute(field)
        if a.kind != "Boolean" or a.default_value is not default:
            raise ValueError(f"{kind}.{field} has invalid Boolean/default")
    ttl = schemas[RECORD].get_attribute("ttl")
    if ttl.kind != "Number" or ttl.default_value != 3600:
        raise ValueError("DNS record TTL must default to 3600 seconds")
    address = schemas[INBOUND].get_relationship("ip_address")
    if (address.peer, address.cardinality, address.optional, address.kind) != (
        "BuiltinIPAddress",
        "one",
        True,
        "Attribute",
    ):
        raise ValueError("Inbound endpoint IPAM address must be an optional reference")
    endpoints = schemas[RULESET].get_relationship("outbound_endpoints")
    if (
        endpoints.peer,
        endpoints.cardinality,
        endpoints.optional,
        endpoints.kind,
        endpoints.min_count,
        endpoints.max_count,
    ) != (OUTBOUND, "many", False, "Attribute", 1, 2):
        raise ValueError("DNS ruleset must reference one or two outbound endpoints")
    for parent, field, child, back in [
        (ZONE, "links", ZONE_LINK, "zone"),
        (ZONE, "record_sets", RECORD, "zone"),
        (RESOLVER, "inbound_endpoints", INBOUND, "resolver"),
        (RESOLVER, "outbound_endpoints", OUTBOUND, "resolver"),
        (RULESET, "rules", RULE, "ruleset"),
        (RULESET, "links", RULESET_LINK, "ruleset"),
    ]:
        r = schemas[parent].get_relationship(field)
        if (r.peer, r.kind, r.cardinality, r.optional) != (
            child,
            "Component",
            "many",
            True,
        ) or r.identifier != schemas[child].get_relationship(back).identifier:
            raise ValueError(f"{parent}.{field} has invalid DNS child relationship")
    for kind in TAGGABLE:
        if schemas[kind].get_relationship("tags").peer != "AzureTag":
            raise ValueError(f"{kind} must support Azure tags")


def verify_storage(schemas):
    if __package__:
        from .check_azure_storage import (
            ACCOUNT,
            CONTAINER,
            BACKEND,
            ACCOUNT_RE,
            CONTAINER_RE,
            CHOICES,
            BOOLEANS,
            RETENTIONS,
        )
    else:
        from check_azure_storage import (
            ACCOUNT,
            CONTAINER,
            BACKEND,
            ACCOUNT_RE,
            CONTAINER_RE,
            CHOICES,
            BOOLEANS,
            RETENTIONS,
        )
    account, container, backend = (schemas[k] for k in (ACCOUNT, CONTAINER, BACKEND))
    if (
        set(account.inherit_from) != {"AzureResource", "AzureTaggable"}
        or container.inherit_from
        or backend.inherit_from
    ):
        raise ValueError(
            "Azure storage inheritance must reflect resource and container ownership"
        )
    for kind, low, high, regex, unique in [
        (ACCOUNT, 3, 24, ACCOUNT_RE, True),
        (CONTAINER, 3, 63, CONTAINER_RE, False),
        (BACKEND, 1, 128, None, False),
    ]:
        node = schemas[kind]
        name = node.get_attribute("name")
        if (
            name.kind != "Text"
            or name.optional
            or name.min_length != low
            or name.max_length != high
            or name.regex != regex
            or name.unique != unique
        ):
            raise ValueError(f"{kind}.name has invalid constraints")
        if node.display_label != "name__value":
            raise ValueError(f"{kind} must display its name")
    if container.uniqueness_constraints != [
        ["storage_account", "name__value"]
    ] or backend.uniqueness_constraints != [["container", "key__value"]]:
        raise ValueError("Azure storage container/backend uniqueness has invalid scope")
    for kind, field, choices, default in [
        (ACCOUNT, k, v, v[0]) for k, v in CHOICES.items()
    ] + [
        (CONTAINER, "public_access", ("private",), "private"),
        (BACKEND, "authentication", ("entra_id",), "entra_id"),
    ]:
        a = schemas[kind].get_attribute(field)
        if (
            a.kind != "Dropdown"
            or a.default_value != default
            or {v["name"] for v in a.choices or []} != set(choices)
        ):
            raise ValueError(f"{kind}.{field} has invalid choices/default")
    for field, default in BOOLEANS.items():
        a = account.get_attribute(field)
        # Server normalizes defaulted attributes to optional.
        if a.kind != "Boolean" or a.default_value is not default:
            raise ValueError(f"{ACCOUNT}.{field} has invalid Boolean/default")
    for field in RETENTIONS:
        a = account.get_attribute(field)
        if a.kind != "Number" or not a.optional or a.default_value != 7:
            raise ValueError(
                f"{ACCOUNT}.{field} must default to seven days and allow null"
            )
    key = backend.get_attribute("key")
    if (
        key.kind != "Text"
        or key.optional
        or key.min_length != 1
        or key.max_length != 1024
    ):
        raise ValueError(f"{BACKEND}.key must be required Text of 1–1024 characters")
    for kind, field, peer, relkind, many, optional in [
        (ACCOUNT, "resourcegroup", "AzureResourceGroup", "Parent", False, False),
        (ACCOUNT, "location", "AzureRegion", "Attribute", False, False),
        (ACCOUNT, "containers", CONTAINER, "Component", True, True),
        (CONTAINER, "storage_account", ACCOUNT, "Parent", False, False),
        (CONTAINER, "state_backends", BACKEND, "Generic", True, True),
        (BACKEND, "container", CONTAINER, "Attribute", False, False),
    ]:
        r = schemas[kind].get_relationship(field)
        if (r.peer, r.kind, r.cardinality, r.optional) != (
            peer,
            relkind,
            "many" if many else "one",
            optional,
        ):
            raise ValueError(f"{kind}.{field} has invalid relationship")
    if (
        account.get_relationship("containers").identifier
        != container.get_relationship("storage_account").identifier
        or container.get_relationship("state_backends").identifier
        != backend.get_relationship("container").identifier
    ):
        raise ValueError("Azure storage relationship identifiers must match")


def verify_peering(schemas):
    node = schemas["AzureVirtualNetworkPeering"]
    if node.inherit_from or node.uniqueness_constraints != [
        ["virtual_network_a", "virtual_network_b"]
    ]:
        raise ValueError(
            "AzureVirtualNetworkPeering must be independent with ordered-pair uniqueness"
        )
    if (
        node.display_label
        != "{{ peering_name_a__value }} ↔ {{ peering_name_b__value }}"
    ):
        raise ValueError("AzureVirtualNetworkPeering must display both names")
    identifiers = set()
    for side in "ab":
        name = node.get_attribute(f"peering_name_{side}")
        if (
            name.kind != "Text"
            or name.optional
            or name.unique
            or name.min_length != 1
            or name.max_length != 80
            or name.regex != r"^[A-Za-z0-9](?:[A-Za-z0-9_.-]*[A-Za-z0-9_])?\Z"
        ):
            raise ValueError(
                "AzureVirtualNetworkPeering names must enforce Azure naming rules"
            )
        for option in (
            "allow_virtual_network_access",
            "allow_forwarded_traffic",
            "allow_gateway_transit",
            "use_remote_gateways",
        ):
            attr = node.get_attribute(f"{side}_{option}")
            # Infrahub normalizes defaulted attributes to optional on read.
            if attr.kind != "Boolean" or attr.default_value is not (
                option == "allow_virtual_network_access"
            ):
                raise ValueError(
                    f"AzureVirtualNetworkPeering.{side}_{option} has invalid definition/default"
                )
        peer = node.get_relationship(f"virtual_network_{side}")
        reverse = schemas["AzureVirtualNetwork"].get_relationship(f"peerings_{side}")
        if (
            (peer.peer, peer.kind, peer.cardinality, peer.optional)
            != ("AzureVirtualNetwork", "Attribute", "one", False)
            or (reverse.peer, reverse.kind, reverse.cardinality, reverse.optional)
            != ("AzureVirtualNetworkPeering", "Generic", "many", True)
            or peer.identifier != reverse.identifier
        ):
            raise ValueError(
                "AzureVirtualNetworkPeering requires reciprocal VNet references"
            )
        identifiers.add(peer.identifier)
    if len(identifiers) != 2:
        raise ValueError("AzureVirtualNetworkPeering end identifiers must differ")


def verify_service_endpoints(schemas):
    if __package__:
        from .check_azure_networks import SERVICE_ENDPOINTS
    else:
        from check_azure_networks import SERVICE_ENDPOINTS
    node = schemas["AzureSubnetServiceEndpoint"]
    service = node.get_attribute_or_none("service_name")
    if (
        service is None
        or service.kind != "Dropdown"
        or service.optional
        or {c["name"] for c in service.choices or []} != set(SERVICE_ENDPOINTS)
    ):
        raise ValueError(
            "AzureSubnetServiceEndpoint.service_name must expose the supported service choices"
        )
    key = node.get_attribute_or_none("service_key")
    if (
        key is None
        or key.kind != "Text"
        or key.optional
        or not key.read_only
        or key.unique
    ):
        raise ValueError(
            "AzureSubnetServiceEndpoint.service_key must be required read-only scoped Text"
        )
    if node.uniqueness_constraints != [["subnet", "service_key__value"]]:
        raise ValueError(
            "AzureSubnetServiceEndpoint uniqueness must be scoped to subnet and service"
        )
    owner = node.get_relationship("subnet")
    reverse = schemas["AzureVirtualNetworkSubnet"].get_relationship("service_endpoints")
    if (owner.peer, owner.kind, owner.cardinality, owner.optional) != (
        "AzureVirtualNetworkSubnet",
        "Parent",
        "one",
        False,
    ):
        raise ValueError("AzureSubnetServiceEndpoint.subnet must be a required parent")
    if (reverse.peer, reverse.kind, reverse.cardinality, reverse.optional) != (
        "AzureSubnetServiceEndpoint",
        "Component",
        "many",
        True,
    ) or owner.identifier != reverse.identifier:
        raise ValueError(
            "AzureVirtualNetworkSubnet.service_endpoints must expose owned selections"
        )
    if node.display_label != "service_name__value":
        raise ValueError("AzureSubnetServiceEndpoint must display its service name")


def verify_subnet_delegation(schemas):
    node = schemas["AzureSubnetDelegation"]
    for key in ("name_key", "service_key"):
        a = node.get_attribute_or_none(key)
        if a is None or a.kind != "Text" or a.optional or not a.read_only or a.unique:
            raise ValueError(
                f"AzureSubnetDelegation.{key} must be required read-only scoped Text"
            )
        if ["subnet", f"{key}__value"] not in (node.uniqueness_constraints or []):
            raise ValueError(
                f"AzureSubnetDelegation.{key} uniqueness must be subnet scoped"
            )
    for field in ("name", "service_name"):
        a = node.get_attribute_or_none(field)
        if a is None or a.kind != "Text" or a.optional or not a.regex:
            raise ValueError(
                f"AzureSubnetDelegation.{field} must be required validated Text"
            )
    forward = node.get_relationship("subnet")
    reverse = schemas["AzureVirtualNetworkSubnet"].get_relationship("delegations")
    if (forward.peer, forward.kind, forward.cardinality, forward.optional) != (
        "AzureVirtualNetworkSubnet",
        "Parent",
        "one",
        False,
    ):
        raise ValueError("AzureSubnetDelegation.subnet must be a required parent")
    if (reverse.peer, reverse.kind, reverse.cardinality, reverse.optional) != (
        "AzureSubnetDelegation",
        "Component",
        "many",
        True,
    ) or forward.identifier != reverse.identifier:
        raise ValueError(
            "AzureVirtualNetworkSubnet.delegations must expose owned delegations"
        )


def verify_network_policy(schemas):
    parents = {
        "AzureVirtualNetworkSubnet": ("virtualnetwork", "AzureVirtualNetwork"),
        "AzureNetworkSecurityGroup": ("resourcegroup", "AzureResourceGroup"),
        "AzureRouteTable": ("resourcegroup", "AzureResourceGroup"),
        "AzureNetworkSecurityRule": (
            "network_security_group",
            "AzureNetworkSecurityGroup",
        ),
        "AzureRoute": ("route_table", "AzureRouteTable"),
    }

    def relationship(kind, name, peer, cardinality, optional):
        r = schemas[kind].get_relationship_or_none(name)
        if r is None or (r.peer, r.cardinality, r.optional) != (
            peer,
            cardinality,
            optional,
        ):
            raise ValueError(f"{kind}.{name} has an invalid network relationship")
        return r

    for kind, (parent, peer) in parents.items():
        node = schemas[kind]
        if [parent, "name_key__value"] not in (node.uniqueness_constraints or []):
            raise ValueError(f"{kind} must scope name uniqueness to {parent}")
        key = node.get_attribute_or_none("name_key")
        if (
            key is None
            or key.kind != "Text"
            or key.optional
            or key.unique
            or not key.read_only
        ):
            raise ValueError(f"{kind}.name_key must be required read-only scoped Text")
        name = node.get_attribute("name")
        if (
            name.optional
            or name.unique
            or name.min_length != 1
            or name.max_length != 80
            or name.regex != r"^[A-Za-z0-9](?:[A-Za-z0-9_.-]*[A-Za-z0-9_])?\Z"
        ):
            raise ValueError(f"{kind}.name must follow Azure naming rules")
        if relationship(kind, parent, peer, "one", False).kind != "Parent":
            raise ValueError(f"{kind}.{parent} must be Parent")
    for kind in ("AzureNetworkSecurityGroup", "AzureRouteTable"):
        if set(schemas[kind].inherit_from) != {"AzureResource", "AzureTaggable"}:
            raise ValueError(f"{kind} must inherit AzureResource and AzureTaggable")
        if (
            relationship(kind, "location", "AzureRegion", "one", False).label
            != "Region"
        ):
            raise ValueError(f"{kind}.location must be labeled Region")
    sub = "AzureVirtualNetworkSubnet"
    if (
        relationship(
            sub, "address_prefixes", "BuiltinIPPrefix", "many", False
        ).min_count
        != 1
    ):
        raise ValueError(
            "AzureVirtualNetworkSubnet.address_prefixes must require at least one prefix"
        )
    for field, kind in [
        ("network_security_group", "AzureNetworkSecurityGroup"),
        ("route_table", "AzureRouteTable"),
    ]:
        forward = relationship(sub, field, kind, "one", True)
        reverse = relationship(kind, "subnets", sub, "many", True)
        if not forward.identifier or forward.identifier != reverse.identifier:
            raise ValueError(f"{kind}.subnets must reverse the subnet association")
    for kind, field, child in [
        ("AzureNetworkSecurityGroup", "rules", "AzureNetworkSecurityRule"),
        ("AzureRouteTable", "routes", "AzureRoute"),
    ]:
        if relationship(kind, field, child, "many", True).kind != "Component":
            raise ValueError(f"{kind}.{field} must own child records")
    rule = schemas["AzureNetworkSecurityRule"]
    if [
        "network_security_group",
        "direction__value",
        "priority__value",
    ] not in rule.uniqueness_constraints:
        raise ValueError(
            "AzureNetworkSecurityRule must scope priority uniqueness to NSG and direction"
        )
    if rule.order_by != ["direction__value", "priority__value"]:
        raise ValueError(
            "AzureNetworkSecurityRule must order by direction and priority"
        )
    for kind, attrs in {
        "AzureNetworkSecurityRule": {
            "priority": "Number",
            "source_addresses": "Text",
            "destination_addresses": "Text",
            "source_ports": "Text",
            "destination_ports": "Text",
        },
        "AzureRoute": {"address_prefix": "Text"},
    }.items():
        for field, expected in attrs.items():
            a = schemas[kind].get_attribute_or_none(field)
            if a is None or a.kind != expected or a.optional:
                raise ValueError(f"{kind}.{field} must be required {expected}")
    for kind, field, choices in [
        ("AzureNetworkSecurityRule", "direction", {"inbound", "outbound"}),
        ("AzureNetworkSecurityRule", "access", {"allow", "deny"}),
        (
            "AzureNetworkSecurityRule",
            "protocol",
            {"any", "tcp", "udp", "icmp", "esp", "ah"},
        ),
        (
            "AzureRoute",
            "next_hop_type",
            {
                "internet",
                "none",
                "virtual_appliance",
                "virtual_network_gateway",
                "vnet_local",
            },
        ),
    ]:
        a = schemas[kind].get_attribute_or_none(field)
        if (
            a is None
            or a.kind != "Dropdown"
            or a.optional
            or {c["name"] for c in a.choices or []} != choices
        ):
            raise ValueError(f"{kind}.{field} has invalid choices")
    for kind, field, maximum in [
        ("AzureNetworkSecurityRule", "description", 140),
        ("AzureRoute", "next_hop_ip_address", None),
    ]:
        a = schemas[kind].get_attribute_or_none(field)
        if (
            a is None
            or a.kind != "Text"
            or not a.optional
            or (maximum is not None and a.max_length != maximum)
        ):
            raise ValueError(f"{kind}.{field} has an invalid optional Text definition")
    propagation = schemas["AzureRouteTable"].get_attribute_or_none(
        "disable_bgp_route_propagation"
    )
    if (
        propagation is None
        or propagation.kind != "Boolean"
        or propagation.default_value is not False
    ):
        raise ValueError("AzureRouteTable must enable BGP route propagation by default")


def verify_tags(schemas) -> None:
    for kind in ("AzureTag", "AzureTaggable"):
        if kind not in schemas:
            raise ValueError(f"{kind} is missing")
    tag = schemas["AzureTag"]
    for name in ("key", "value"):
        attribute = tag.get_attribute_or_none(name)
        if attribute is None or attribute.kind != "Text" or attribute.optional:
            raise ValueError(f"AzureTag.{name} must be required Text")
    if not any(set(c) == {"owner", "key__value"} for c in tag.uniqueness_constraints):
        raise ValueError("AzureTag uniqueness must be scoped to owner and key")
    owner = tag.get_relationship("owner")
    if (
        owner.peer != "AzureTaggable"
        or owner.cardinality != "one"
        or owner.optional
        or owner.kind != "Parent"
    ):
        raise ValueError("AzureTag.owner must be one required AzureTaggable parent")
    supported = {
        "AzureSubscription",
        "AzureResourceGroup",
        "AzureVirtualNetwork",
        "AzureNetworkSecurityGroup",
        "AzureRouteTable",
        "AzureStorageAccount",
        "AzurePrivateDnsZone",
        "AzureDnsPrivateResolver",
        "AzureDnsForwardingRuleset",
    }
    for kind in (*supported, "AzureTaggable"):
        rel = schemas[kind].get_relationship_or_none("tags")
        if (
            rel is None
            or rel.peer != "AzureTag"
            or rel.cardinality != "many"
            or not rel.optional
            or rel.kind != "Component"
            or rel.max_count != 50
            or rel.identifier != owner.identifier
        ):
            raise ValueError(f"{kind}.tags must expose up to 50 owned Azure tags")
    for kind in AZURE_NODES:
        if ("AzureTaggable" in schemas[kind].inherit_from) != (kind in supported):
            raise ValueError(f"{kind} has incorrect Azure tag support")
    for kind in ("AzureRegion", "LocationGroup"):
        if schemas[kind].get_relationship("tags").peer != "BuiltinTag":
            raise ValueError(f"{kind} must preserve BuiltinTag labels")


def verify(client: InfrahubClientSync, branch: str) -> None:
    schemas = client.schema.all(branch=branch, refresh=True)
    verify_azure(schemas)
    verify_tags(schemas)
    expected = {
        "IpamNamespace": "BuiltinIPNamespace",
        "IpamPrefix": "BuiltinIPPrefix",
        "IpamIPAddress": "BuiltinIPAddress",
    }
    for kind, generic in expected.items():
        if kind not in schemas or generic not in schemas[kind].inherit_from:
            raise ValueError(f"{kind} must exist and inherit from {generic}")
    if "IpamVRF" not in schemas:
        raise ValueError("IpamVRF is missing")
    if "IpamRouteTarget" not in schemas:
        raise ValueError("IpamRouteTarget is missing")
    for relationship in ("import_rt", "export_rt"):
        if schemas["IpamVRF"].get_relationship(relationship).peer != "IpamRouteTarget":
            raise ValueError(f"IpamVRF.{relationship} must refer to IpamRouteTarget")
    if schemas["IpamVRF"].get_attribute("enforce_unique").kind != "Boolean":
        raise ValueError(
            "IpamVRF.enforce_unique must retain the upstream Boolean field"
        )
    for kind, field in (("IpamPrefix", "prefix"), ("IpamIPAddress", "address")):
        schema = schemas[kind]
        identity = {f"{field}__value", "ip_namespace"}
        if not any(
            set(constraint) == identity for constraint in schema.uniqueness_constraints
        ):
            raise ValueError(f"{kind} must scope address uniqueness to ip_namespace")
        namespace = schema.get_relationship("ip_namespace")
        if namespace.peer != "BuiltinIPNamespace":
            raise ValueError(f"{kind} must use the native IP namespace relationship")
        vrf = schema.get_relationship("vrf")
        if vrf.peer != "IpamVRF" or not vrf.optional or vrf.cardinality != "one":
            raise ValueError(f"{kind}.vrf must be optional and refer to one IpamVRF")
    # Verify API exposure as well as the REST schema, without creating sample data.
    counts = client.execute_graphql(
        query="{ IpamNamespace { count } IpamPrefix { count } "
        "IpamIPAddress { count } IpamVRF { count } IpamRouteTarget { count } "
        + " ".join(
            f"{kind} {{ count }}"
            for kind in (*AZURE_NODES, "LocationGroup", "AzureTag")
        )
        + " }",
        branch_name=branch,
    )
    print(f"IPAM and Azure schemas verified on branch {branch}.")
    for kind, result in counts.items():
        print(f"  {kind}: {result['count']} objects")


@app.command()
def main(
    branch: Annotated[str, typer.Option(help="Infrahub branch to inspect")],
) -> None:
    try:
        verify(InfrahubClientSync(), branch)
    except Exception as exc:
        # Avoid dumping SDK request/response details that could contain credentials.
        detail = str(exc) if type(exc) is ValueError else type(exc).__name__
        print(f"Schema verification failed: {detail}", file=sys.stderr)
        raise typer.Exit(code=1) from None


if __name__ == "__main__":
    app()
