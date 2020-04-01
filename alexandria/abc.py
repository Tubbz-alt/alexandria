from abc import ABC, abstractmethod
import ipaddress
from typing import (
    Any,
    AsyncContextManager,
    AsyncIterable,
    Awaitable,
    Callable,
    ContextManager,
    Generic,
    NamedTuple,
    Optional,
    Tuple,
    Type,
    TypeVar,
)

from ssz import sedes

from async_service import ServiceAPI
from eth_keys import keys

from .typing import NodeID, Tag


class PacketAPI(ABC):
    packet_id: int
    tag: Tag

    @abstractmethod
    def to_wire_bytes(self) -> bytes:
        ...

    @classmethod
    @abstractmethod
    def from_wire_bytes(cls: Type['TPacket'], data: bytes) -> 'TPacket':
        ...


TPacket = TypeVar('TPacket', bound=PacketAPI)


class Endpoint(NamedTuple):
    ip_address: ipaddress.IPv4Address
    port: int

    def __str__(self):
        return f"{self.ip_address}:{self.port}"


class Datagram(NamedTuple):
    data: bytes
    endpoint: Endpoint


TPayload = TypeVar('TPayload', bound=sedes.Serializable)


class RegistryAPI(ABC):
    @abstractmethod
    def register(self, message_id: int) -> Callable[[Type[TPayload]], Type[TPayload]]:
        ...


class MessageAPI(Generic[TPayload]):
    message_id: int
    payload: TPayload
    node_id: NodeID
    endpoint: Endpoint

    @abstractmethod
    def to_bytes(self) -> bytes:
        ...


class SessionAPI(ABC):
    @abstractmethod
    def __init__(self,
                 local_private_key: keys.PrivateKey,
                 remote_node_id: NodeID,
                 ) -> None:
        ...

    @abstractmethod
    async def handle_outbound_message(self, message: MessageAPI) -> None:
        ...

    @abstractmethod
    async def handle_inbound_packet(self, packet: PacketAPI) -> Optional[PacketAPI]:
        ...

    @abstractmethod
    async def handle_outbound_packet(self, packet: PacketAPI) -> PacketAPI:
        ...

    @property
    @abstractmethod
    def is_before_handshake(self) -> bool:
        ...

    @property
    @abstractmethod
    def is_handshake_complete(self) -> bool:
        ...

    @property
    @abstractmethod
    def is_during_handshake(self) -> bool:
        ...

    @property
    @abstractmethod
    def private_key(self) -> keys.PrivateKey:
        """The static node key of this node."""
        ...

    @property
    @abstractmethod
    def local_node_id(self) -> NodeID:
        """The node id of this node."""
        ...

    @property
    @abstractmethod
    def remote_node_id(self) -> NodeID:
        """The peer's node id."""
        ...

    @property
    @abstractmethod
    def remote_endpoint(self) -> Endpoint:
        """The peer's endpoint."""
        ...

    @property
    @abstractmethod
    def tag(self) -> Tag:
        """The tag used for message packets sent by this node to the peer."""
        ...


class PoolAPI(ABC):
    @abstractmethod
    def get_session(self, remote_node_id: NodeID) -> SessionAPI:
        ...

    @abstractmethod
    def create_session(self, remote_node_id: NodeID, remote_endpoint: Endpoint) -> SessionAPI:
        ...


TItem = TypeVar('TItem')


class SubscriptionAPI(ContextManager['SubscriptionAPI["TItem"]'], Generic[TItem]):
    @abstractmethod
    async def receive(self) -> TItem:
        ...

    @abstractmethod
    async def stream(self) -> AsyncIterable[TItem]:
        ...


class NodeAPI(ABC):
    id: NodeID
    address: ipaddress.IPv4Address
    port: int

    @property
    @abstractmethod
    def public_key(self) -> keys.PublicKey:
        ...


TAwaitable = TypeVar('TAwaitable', bound=Awaitable[Any])


class EventSubscriptionAPI(Awaitable[TAwaitable], AsyncContextManager[Awaitable[TAwaitable]]):
    pass


class EventsAPI(ABC):
    @abstractmethod
    async def new_session(self, session: SessionAPI) -> None:
        ...

    @abstractmethod
    async def wait_new_session(self) -> EventSubscriptionAPI[SessionAPI]:
        ...


class ClientAPI(ServiceAPI):
    events: EventsAPI

    @abstractmethod
    def subscribe(self, payload_type: Type[TPayload]) -> SubscriptionAPI[MessageAPI[TPayload]]:
        ...


class NodeDatabaseAPI(ABC):
    @abstractmethod
    async def get_bootnodes(self) -> Tuple[NodeID, ...]:
        ...
