from __future__ import annotations
from collections.abc import Callable
from dataclasses import dataclass, field
from kiota_abstractions.serialization import AdditionalDataHolder, Parsable, ParseNode, SerializationWriter
from typing import Any, Optional, TYPE_CHECKING, Union

if TYPE_CHECKING:
    from .data_source import DataSource
    from .edge import Edge
    from .node import Node

@dataclass
class ProvenanceAnalyticalPattern(AdditionalDataHolder, Parsable):
    # Stores additional data not described in the OpenAPI description found when deserializing. Can be used for serialization as well.
    additional_data: dict[str, Any] = field(default_factory=dict)

    # The data_source property
    data_source: Optional[DataSource] = None
    # The edges property
    edges: Optional[list[Edge]] = None
    # The nodes property
    nodes: Optional[list[Node]] = None
    
    @staticmethod
    def create_from_discriminator_value(parse_node: ParseNode) -> ProvenanceAnalyticalPattern:
        """
        Creates a new instance of the appropriate class based on discriminator value
        param parse_node: The parse node to use to read the discriminator value and create the object
        Returns: ProvenanceAnalyticalPattern
        """
        if parse_node is None:
            raise TypeError("parse_node cannot be null.")
        return ProvenanceAnalyticalPattern()
    
    def get_field_deserializers(self,) -> dict[str, Callable[[ParseNode], None]]:
        """
        The deserialization information for the current model
        Returns: dict[str, Callable[[ParseNode], None]]
        """
        from .data_source import DataSource
        from .edge import Edge
        from .node import Node

        from .data_source import DataSource
        from .edge import Edge
        from .node import Node

        fields: dict[str, Callable[[Any], None]] = {
            "data_source": lambda n : setattr(self, 'data_source', n.get_object_value(DataSource)),
            "edges": lambda n : setattr(self, 'edges', n.get_collection_of_object_values(Edge)),
            "nodes": lambda n : setattr(self, 'nodes', n.get_collection_of_object_values(Node)),
        }
        return fields
    
    def serialize(self,writer: SerializationWriter) -> None:
        """
        Serializes information the current object
        param writer: Serialization writer to use to serialize this model
        Returns: None
        """
        if writer is None:
            raise TypeError("writer cannot be null.")
        writer.write_object_value("data_source", self.data_source)
        writer.write_collection_of_object_values("edges", self.edges)
        writer.write_collection_of_object_values("nodes", self.nodes)
        writer.write_additional_data_value(self.additional_data)
    

