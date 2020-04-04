import logging
import secrets
from typing import Iterable, Tuple

from async_service import Service
from eth_utils import to_tuple
import trio

from alexandria._utils import every, humanize_node_id
from alexandria.abc import (
    ClientAPI,
    EndpointDatabaseAPI,
    NetworkAPI,
    Node,
    RoutingTableAPI,
)
from alexandria.constants import PING_TIMEOUT
from alexandria.payloads import FindNodes, Ping
from alexandria.typing import NodeID


ROUTING_TABLE_PING_INTERVAL = 30  # interval of outgoing pings sent to maintain the routing table
ROUTING_TABLE_LOOKUP_INTERVAL = 10  # intervals between lookups


NodePayload = Tuple[NodeID, bytes, int]


class _EmptyFindNodesResponse(Exception):
    pass


class RoutingTableManager(Service):
    logger = logging.getLogger('alexandria.routing_table_manager.RoutingTableManager')

    def __init__(self,
                 routing_table: RoutingTableAPI,
                 endpoint_db: EndpointDatabaseAPI,
                 client: ClientAPI,
                 network: NetworkAPI,
                 ) -> None:
        self.endpoint_db = endpoint_db
        self.routing_table = routing_table
        self.client = client
        self.network = network

    async def run(self) -> None:
        self.manager.run_daemon_task(self._periodic_report_routing_table_status)
        self.manager.run_daemon_task(self._pong_when_pinged)
        self.manager.run_daemon_task(self._ping_occasionally)
        self.manager.run_daemon_task(self._lookup_occasionally)
        self.manager.run_daemon_task(self._handle_lookup_requests)
        await self.manager.wait_finished()

    async def _periodic_report_routing_table_status(self) -> None:
        async for _ in every(30, 10):  # noqa: F841
            stats = self.routing_table.get_stats()
            if stats.full_buckets:
                full_buckets = '/'.join((str(index) for index in stats.full_buckets))
            else:
                full_buckets = 'None'
            self.logger.info(
                (
                    "\n"
                    "####################################################\n"
                    "       RoutingTable(bucket_size=%d, num_buckets=%d):\n"
                    "         - %d nodes\n"
                    "         - full buckets: %s\n"
                    "         - %d nodes in replacement cache\n"
                    "####################################################"
                ),
                stats.bucket_size,
                stats.num_buckets,
                stats.total_nodes,
                full_buckets,
                stats.num_in_replacement_cache,
            )

    async def _handle_lookup_requests(self) -> None:
        with self.client.message_dispatcher.subscribe(FindNodes) as subscription:
            while self.manager.is_running:
                request = await subscription.receive()
                self.logger.debug("handling request: %s", request)

                if request.payload.distance == 0:
                    found_nodes = (self.client.local_node,)
                else:
                    found_nodes = self._get_nodes_at_distance(request.payload.distance)

                self.logger.debug(
                    'found %d nodes for request: %s',
                    len(found_nodes),
                    request,
                )
                await self.client.send_found_nodes(
                    request.node,
                    request_id=request.payload.request_id,
                    found_nodes=found_nodes,
                )

    @to_tuple
    def _get_nodes_at_distance(self, distance: int) -> Iterable[Node]:
        """Send a Nodes message containing ENRs of peers at a given node distance."""
        nodes_at_distance = self.routing_table.get_nodes_at_log_distance(distance)

        for node_id in nodes_at_distance:
            endpoint = self.endpoint_db.get_endpoint(node_id)
            yield Node(node_id, endpoint)

    async def _verify_node(self, node: Node) -> bool:
        with trio.move_on_after(PING_TIMEOUT) as scope:
            await self.network.verify_and_add(node)

        if scope.cancelled_caught:
            self.logger.debug('node verification timed out: %s', node)
            return False
        else:
            self.logger.debug('node verification succeeded: %s', node)
            return True

    async def _lookup_occasionally(self) -> None:
        async with trio.open_nursery() as nursery:
            async for _ in every(ROUTING_TABLE_LOOKUP_INTERVAL):  # noqa: F841
                if self.routing_table.is_empty:
                    self.logger.debug('Aborting scheduled lookup due to empty routing table')
                    continue

                target_node_id = NodeID(secrets.randbits(256))
                found_nodes = await self.network.iterative_lookup(target_node_id)
                self.logger.debug(
                    'Lookup for %s yielded %d nodes',
                    humanize_node_id(target_node_id),
                    len(found_nodes),
                )
                for node in found_nodes:
                    nursery.start_soon(self._verify_node, node)

    async def _ping_occasionally(self) -> None:
        async for _ in every(ROUTING_TABLE_PING_INTERVAL):  # noqa: F841
            if self.routing_table.is_empty:
                self.logger.warning("Routing table is empty, no one to ping")
                continue

            log_distance = self.routing_table.get_least_recently_updated_log_distance()
            candidates = self.routing_table.get_nodes_at_log_distance(log_distance)
            for node_id in reversed(candidates):
                endpoint = self.endpoint_db.get_endpoint(node_id)
                node = Node(node_id, endpoint)

                with trio.move_on_after(PING_TIMEOUT) as scope:
                    await self.client.ping(node)

                if scope.cancelled_caught:
                    self.logger.debug(
                        'Node %s did not respond to ping.  Removing from routing table',
                        node_id,
                    )
                    self.routing_table.remove(node_id)
                else:
                    break

    async def _pong_when_pinged(self) -> None:
        with self.client.message_dispatcher.subscribe(Ping) as subscription:
            while self.manager.is_running:
                request = await subscription.receive()
                self.logger.debug(
                    "Got ping from %s, responding with pong",
                    request.node,
                )
                await self.client.send_pong(request.node, request_id=request.payload.request_id)
