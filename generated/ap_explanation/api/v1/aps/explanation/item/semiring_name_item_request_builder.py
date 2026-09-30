from __future__ import annotations
from collections.abc import Callable
from dataclasses import dataclass, field
from kiota_abstractions.base_request_builder import BaseRequestBuilder
from kiota_abstractions.base_request_configuration import RequestConfiguration
from kiota_abstractions.default_query_parameters import QueryParameters
from kiota_abstractions.get_path_parameters import get_path_parameters
from kiota_abstractions.method import Method
from kiota_abstractions.request_adapter import RequestAdapter
from kiota_abstractions.request_information import RequestInformation
from kiota_abstractions.request_option import RequestOption
from kiota_abstractions.serialization import Parsable, ParsableFactory
from typing import Any, Optional, TYPE_CHECKING, Union
from warnings import warn

if TYPE_CHECKING:
    from ......models.h_t_t_p_validation_error import HTTPValidationError
    from ......models.managed_provenance_task_response import ManagedProvenanceTaskResponse
    from ......models.provenance_analytical_pattern import ProvenanceAnalyticalPattern
    from ......models.task_status_response import TaskStatusResponse

class Semiring_nameItemRequestBuilder(BaseRequestBuilder):
    """
    Builds and executes requests for operations under /api/v1/aps/explanation/{semiring_name-id}
    """
    def __init__(self,request_adapter: RequestAdapter, path_parameters: Union[str, dict[str, Any]]) -> None:
        """
        Instantiates a new Semiring_nameItemRequestBuilder and sets the default values.
        param path_parameters: The raw url or the url-template parameters for the request.
        param request_adapter: The request adapter to use to execute the requests.
        Returns: None
        """
        super().__init__(request_adapter, "{+baseurl}/api/v1/aps/explanation/{semiring_name%2Did}{?probability*}", path_parameters)
    
    async def get(self,request_configuration: Optional[RequestConfiguration[QueryParameters]] = None) -> Optional[TaskStatusResponse]:
        """
        Return the current status and (if completed) the result of a managed explanation task.Poll this endpoint after POST /aps/explanation or POST /aps/explanation/{semiring_name}.
        param request_configuration: Configuration for the request such as headers, query parameters, and middleware options.
        Returns: Optional[TaskStatusResponse]
        """
        request_info = self.to_get_request_information(
            request_configuration
        )
        from ......models.h_t_t_p_validation_error import HTTPValidationError

        error_mapping: dict[str, type[ParsableFactory]] = {
            "422": HTTPValidationError,
        }
        if not self.request_adapter:
            raise Exception("Http core is null") 
        from ......models.task_status_response import TaskStatusResponse

        return await self.request_adapter.send_async(request_info, TaskStatusResponse, error_mapping)
    
    async def post(self,body: ProvenanceAnalyticalPattern, request_configuration: Optional[RequestConfiguration[Semiring_nameItemRequestBuilderPostQueryParameters]] = None) -> Optional[ManagedProvenanceTaskResponse]:
        """
        Dispatch an async task for the full explanation lifecycle with a specific semiring.Returns HTTP 202 Accepted. Poll the result at GET /api/v1/aps/explanation/{task_id}.
        param body: The request body
        param request_configuration: Configuration for the request such as headers, query parameters, and middleware options.
        Returns: Optional[ManagedProvenanceTaskResponse]
        """
        if body is None:
            raise TypeError("body cannot be null.")
        request_info = self.to_post_request_information(
            body, request_configuration
        )
        from ......models.h_t_t_p_validation_error import HTTPValidationError

        error_mapping: dict[str, type[ParsableFactory]] = {
            "422": HTTPValidationError,
        }
        if not self.request_adapter:
            raise Exception("Http core is null") 
        from ......models.managed_provenance_task_response import ManagedProvenanceTaskResponse

        return await self.request_adapter.send_async(request_info, ManagedProvenanceTaskResponse, error_mapping)
    
    def to_get_request_information(self,request_configuration: Optional[RequestConfiguration[QueryParameters]] = None) -> RequestInformation:
        """
        Return the current status and (if completed) the result of a managed explanation task.Poll this endpoint after POST /aps/explanation or POST /aps/explanation/{semiring_name}.
        param request_configuration: Configuration for the request such as headers, query parameters, and middleware options.
        Returns: RequestInformation
        """
        request_info = RequestInformation(Method.GET, self.url_template, self.path_parameters)
        request_info.configure(request_configuration)
        request_info.headers.try_add("Accept", "application/json")
        return request_info
    
    def to_post_request_information(self,body: ProvenanceAnalyticalPattern, request_configuration: Optional[RequestConfiguration[Semiring_nameItemRequestBuilderPostQueryParameters]] = None) -> RequestInformation:
        """
        Dispatch an async task for the full explanation lifecycle with a specific semiring.Returns HTTP 202 Accepted. Poll the result at GET /api/v1/aps/explanation/{task_id}.
        param body: The request body
        param request_configuration: Configuration for the request such as headers, query parameters, and middleware options.
        Returns: RequestInformation
        """
        if body is None:
            raise TypeError("body cannot be null.")
        request_info = RequestInformation(Method.POST, self.url_template, self.path_parameters)
        request_info.configure(request_configuration)
        request_info.headers.try_add("Accept", "application/json")
        request_info.set_content_from_parsable(self.request_adapter, "application/json", body)
        return request_info
    
    def with_url(self,raw_url: str) -> Semiring_nameItemRequestBuilder:
        """
        Returns a request builder with the provided arbitrary URL. Using this method means any other path or query parameters are ignored.
        param raw_url: The raw URL to use for the request builder.
        Returns: Semiring_nameItemRequestBuilder
        """
        if raw_url is None:
            raise TypeError("raw_url cannot be null.")
        return Semiring_nameItemRequestBuilder(self.request_adapter, raw_url)
    
    @dataclass
    class Semiring_nameItemRequestBuilderGetRequestConfiguration(RequestConfiguration[QueryParameters]):
        """
        Configuration for the request such as headers, query parameters, and middleware options.
        """
        warn("This class is deprecated. Please use the generic RequestConfiguration class generated by the generator.", DeprecationWarning)
    
    @dataclass
    class Semiring_nameItemRequestBuilderPostQueryParameters():
        """
        Dispatch an async task for the full explanation lifecycle with a specific semiring.Returns HTTP 202 Accepted. Poll the result at GET /api/v1/aps/explanation/{task_id}.
        """
        # Also compute the probability of each result row. Tuple probabilities are read from the column named by each Table/CSV node's `probabilityColumn` property; tables without one are treated as certain.
        probability: Optional[bool] = None

    
    @dataclass
    class Semiring_nameItemRequestBuilderPostRequestConfiguration(RequestConfiguration[Semiring_nameItemRequestBuilderPostQueryParameters]):
        """
        Configuration for the request such as headers, query parameters, and middleware options.
        """
        warn("This class is deprecated. Please use the generic RequestConfiguration class generated by the generator.", DeprecationWarning)
    

