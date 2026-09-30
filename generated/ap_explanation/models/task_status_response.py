from __future__ import annotations
from collections.abc import Callable
from dataclasses import dataclass, field
from kiota_abstractions.serialization import AdditionalDataHolder, Parsable, ParseNode, SerializationWriter
from typing import Any, Optional, TYPE_CHECKING, Union

if TYPE_CHECKING:
    from .task_status_response_error import TaskStatusResponse_error
    from .task_status_response_status import TaskStatusResponse_status

@dataclass
class TaskStatusResponse(AdditionalDataHolder, Parsable):
    # Stores additional data not described in the OpenAPI description found when deserializing. Can be used for serialization as well.
    additional_data: dict[str, Any] = field(default_factory=dict)

    # The error property
    error: Optional[TaskStatusResponse_error] = None
    # The status property
    status: Optional[TaskStatusResponse_status] = None
    # The task_id property
    task_id: Optional[str] = None
    
    @staticmethod
    def create_from_discriminator_value(parse_node: ParseNode) -> TaskStatusResponse:
        """
        Creates a new instance of the appropriate class based on discriminator value
        param parse_node: The parse node to use to read the discriminator value and create the object
        Returns: TaskStatusResponse
        """
        if parse_node is None:
            raise TypeError("parse_node cannot be null.")
        return TaskStatusResponse()
    
    def get_field_deserializers(self,) -> dict[str, Callable[[ParseNode], None]]:
        """
        The deserialization information for the current model
        Returns: dict[str, Callable[[ParseNode], None]]
        """
        from .task_status_response_error import TaskStatusResponse_error
        from .task_status_response_status import TaskStatusResponse_status

        from .task_status_response_error import TaskStatusResponse_error
        from .task_status_response_status import TaskStatusResponse_status

        fields: dict[str, Callable[[Any], None]] = {
            "error": lambda n : setattr(self, 'error', n.get_object_value(TaskStatusResponse_error)),
            "status": lambda n : setattr(self, 'status', n.get_enum_value(TaskStatusResponse_status)),
            "task_id": lambda n : setattr(self, 'task_id', n.get_str_value()),
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
        writer.write_object_value("error", self.error)
        writer.write_enum_value("status", self.status)
        writer.write_str_value("task_id", self.task_id)
        writer.write_additional_data_value(self.additional_data)
    

