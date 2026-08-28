from __future__ import annotations

from datetime import UTC, datetime
from ipaddress import ip_address
from typing import Any

from neo4j import Driver, Record

from bitcoin_intel.graph.models import (
    AddressTransactions,
    AddressTransactionUse,
    GraphAddressUse,
    GraphNodeIdentity,
    GraphObservation,
    GraphPath,
    GraphTransaction,
    IpObservations,
    IpObservationUse,
    TransactionNeighborhood,
)

_NODE_KINDS: dict[str, tuple[str, str]] = {
    "transaction": ("Transaction", "txid"),
    "address": ("Address", "address"),
    "ip": ("IPAddress", "ip"),
    "observation": ("NetworkObservation", "observation_id"),
}


class GraphQueries:
    """Fixed, parameterized and bounded foundational graph queries."""

    def __init__(self, driver: Driver, database: str) -> None:
        self._driver = driver
        self._database = database

    def transaction_neighborhood(self, txid: str) -> TransactionNeighborhood | None:
        normalized_txid = _hex_identifier(txid, "txid")
        with self._driver.session(database=self._database) as session:
            transaction = session.run(
                """MATCH (transaction:Transaction {txid: $txid})
                RETURN transaction.txid AS txid, transaction.fee_sats AS fee_sats,
                transaction.script_type AS script_type""",
                txid=normalized_txid,
            ).single()
            if transaction is None:
                return None
            inputs = tuple(
                GraphAddressUse(
                    address=str(record["address"]),
                    index=int(record["item_index"]),
                    amount_sats=int(record["amount_sats"]),
                )
                for record in session.run(
                    """MATCH (address:Address)-[use:SPENT_IN]->
                    (:Transaction {txid: $txid})
                    RETURN address.address AS address, use.input_index AS item_index,
                    use.amount_sats AS amount_sats ORDER BY item_index, address""",
                    txid=normalized_txid,
                )
            )
            outputs = tuple(
                GraphAddressUse(
                    address=str(record["address"]),
                    index=int(record["item_index"]),
                    amount_sats=int(record["amount_sats"]),
                )
                for record in session.run(
                    """MATCH (:Transaction {txid: $txid})-[use:CREATED_OUTPUT]->
                    (address:Address)
                    RETURN address.address AS address, use.output_index AS item_index,
                    use.amount_sats AS amount_sats ORDER BY item_index, address""",
                    txid=normalized_txid,
                )
            )
            observations = tuple(
                _observation(record)
                for record in session.run(
                    """MATCH (observation:NetworkObservation)-[:OBSERVED_TRANSACTION]->
                    (transaction:Transaction {txid: $txid})
                    MATCH (observation)-[:SOURCE_IP]->(source:IPAddress)
                    MATCH (observation)-[:DESTINATION_IP]->(destination:IPAddress)
                    RETURN observation.observation_id AS observation_id,
                    observation.observed_at AS observed_at,
                    observation.src_port AS src_port, observation.dst_port AS dst_port,
                    observation.reported_geo_country AS reported_geo_country,
                    observation.reported_asn AS reported_asn,
                    observation.source_record_id AS source_record_id,
                    source.ip AS source_ip, destination.ip AS destination_ip,
                    transaction.txid AS txid
                    ORDER BY observed_at, observation_id""",
                    txid=normalized_txid,
                )
            )
        return TransactionNeighborhood(
            transaction=GraphTransaction(
                txid=str(transaction["txid"]),
                fee_sats=int(transaction["fee_sats"]),
                script_type=_optional_str(transaction["script_type"]),
            ),
            inputs=inputs,
            outputs=outputs,
            observations=observations,
        )

    def address_transactions(self, address: str) -> AddressTransactions:
        normalized_address = _nonempty(address, "address")
        query = """
        MATCH (address:Address {address: $address})-[use:SPENT_IN]->(transaction:Transaction)
        RETURN transaction.txid AS txid, 'input' AS role, use.input_index AS item_index,
        use.amount_sats AS amount_sats
        UNION ALL
        MATCH (transaction:Transaction)-[use:CREATED_OUTPUT]->
        (address:Address {address: $address})
        RETURN transaction.txid AS txid, 'output' AS role, use.output_index AS item_index,
        use.amount_sats AS amount_sats
        ORDER BY txid, role, item_index
        """
        with self._driver.session(database=self._database) as session:
            uses = tuple(
                AddressTransactionUse(
                    txid=str(record["txid"]),
                    role=str(record["role"]),
                    index=int(record["item_index"]),
                    amount_sats=int(record["amount_sats"]),
                )
                for record in session.run(query, address=normalized_address)
            )
        return AddressTransactions(normalized_address, uses)

    def ip_observations(self, ip: str) -> IpObservations:
        normalized_ip = str(ip_address(ip.strip()))
        query = """
        MATCH (observation:NetworkObservation)-[:SOURCE_IP]->(:IPAddress {ip: $ip})
        MATCH (observation)-[:DESTINATION_IP]->(destination:IPAddress)
        MATCH (observation)-[:OBSERVED_TRANSACTION]->(transaction:Transaction)
        RETURN 'source' AS role, observation.observation_id AS observation_id,
        observation.observed_at AS observed_at, observation.src_port AS src_port,
        observation.dst_port AS dst_port,
        observation.reported_geo_country AS reported_geo_country,
        observation.reported_asn AS reported_asn,
        observation.source_record_id AS source_record_id,
        $ip AS source_ip, destination.ip AS destination_ip, transaction.txid AS txid
        UNION ALL
        MATCH (observation:NetworkObservation)-[:DESTINATION_IP]->(:IPAddress {ip: $ip})
        MATCH (observation)-[:SOURCE_IP]->(source:IPAddress)
        MATCH (observation)-[:OBSERVED_TRANSACTION]->(transaction:Transaction)
        RETURN 'destination' AS role, observation.observation_id AS observation_id,
        observation.observed_at AS observed_at, observation.src_port AS src_port,
        observation.dst_port AS dst_port,
        observation.reported_geo_country AS reported_geo_country,
        observation.reported_asn AS reported_asn,
        observation.source_record_id AS source_record_id,
        source.ip AS source_ip, $ip AS destination_ip, transaction.txid AS txid
        ORDER BY observed_at, observation_id, role
        """
        with self._driver.session(database=self._database) as session:
            uses = tuple(
                IpObservationUse(str(record["role"]), _observation(record))
                for record in session.run(query, ip=normalized_ip)
            )
        return IpObservations(normalized_ip, uses)

    def shortest_path(
        self,
        source: GraphNodeIdentity,
        target: GraphNodeIdentity,
        *,
        max_depth: int = 4,
    ) -> GraphPath | None:
        source_label, source_key = _node_descriptor(source.kind)
        target_label, target_key = _node_descriptor(target.kind)
        if isinstance(max_depth, bool) or not 1 <= max_depth <= 8:
            raise ValueError("maximum path depth must be an integer from 1 through 8")
        source_value = _node_value(source.kind, source.value)
        target_value = _node_value(target.kind, target.value)
        query = f"""
        MATCH (source:{source_label} {{{source_key}: $source_value}})
        MATCH (target:{target_label} {{{target_key}: $target_value}})
        MATCH path = shortestPath((source)-[*..{max_depth}]-(target))
        RETURN [node IN nodes(path) | {{
            labels: labels(node), txid: node.txid, address: node.address,
            ip: node.ip, observation_id: node.observation_id
        }}] AS nodes,
        [relationship IN relationships(path) | type(relationship)] AS relationship_types
        """
        with self._driver.session(database=self._database) as session:
            record = session.run(
                query, source_value=source_value, target_value=target_value
            ).single()
        if record is None:
            return None
        return GraphPath(
            nodes=tuple(_path_identity(node) for node in record["nodes"]),
            relationship_types=tuple(str(value) for value in record["relationship_types"]),
        )


def _observation(record: Record) -> GraphObservation:
    return GraphObservation(
        observation_id=str(record["observation_id"]),
        observed_at=_datetime(record["observed_at"]),
        src_port=int(record["src_port"]),
        dst_port=int(record["dst_port"]),
        reported_geo_country=_optional_str(record["reported_geo_country"]),
        reported_asn=(None if record["reported_asn"] is None else int(record["reported_asn"])),
        source_record_id=str(record["source_record_id"]),
        source_ip=str(record["source_ip"]),
        destination_ip=str(record["destination_ip"]),
        txid=str(record["txid"]),
    )


def _path_identity(value: Any) -> GraphNodeIdentity:
    if not isinstance(value, dict):
        raise TypeError("Neo4j returned an invalid path node")
    labels = value.get("labels")
    if not isinstance(labels, list) or len(labels) != 1:
        raise TypeError("Neo4j path nodes must have one factual graph label")
    label = str(labels[0])
    mapping = {
        "Transaction": ("transaction", "txid"),
        "Address": ("address", "address"),
        "IPAddress": ("ip", "ip"),
        "NetworkObservation": ("observation", "observation_id"),
    }
    try:
        kind, key = mapping[label]
    except KeyError as error:
        raise TypeError(f"Neo4j returned an unsupported factual node label: {label}") from error
    return GraphNodeIdentity(kind, str(value[key]))


def _node_descriptor(kind: str) -> tuple[str, str]:
    try:
        return _NODE_KINDS[kind]
    except KeyError as error:
        raise ValueError(f"unsupported graph node kind: {kind}") from error


def _node_value(kind: str, value: str) -> str:
    if kind in {"transaction", "observation"}:
        return _hex_identifier(value, f"{kind} identifier")
    if kind == "ip":
        return str(ip_address(value.strip()))
    return _nonempty(value, "address")


def _hex_identifier(value: str, field: str) -> str:
    normalized = value.strip().lower()
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise ValueError(f"{field} must contain exactly 64 hexadecimal characters")
    return normalized


def _nonempty(value: str, field: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field} must not be empty")
    return normalized


def _datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise TypeError("Neo4j returned a naive observation timestamp")
        return value.astimezone(UTC)
    if hasattr(value, "to_native"):
        native = value.to_native()
        if isinstance(native, datetime) and native.tzinfo is not None:
            return native.astimezone(UTC)
    raise TypeError("Neo4j returned an invalid observation timestamp")


def _optional_str(value: Any) -> str | None:
    return None if value is None else str(value)
