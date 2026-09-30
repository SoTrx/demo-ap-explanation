from __future__ import annotations
from collections.abc import Callable
from kiota_abstractions.base_request_builder import BaseRequestBuilder
from kiota_abstractions.get_path_parameters import get_path_parameters
from kiota_abstractions.request_adapter import RequestAdapter
from typing import Any, Optional, TYPE_CHECKING, Union

if TYPE_CHECKING:
    from .aps.aps_request_builder import ApsRequestBuilder
    from .health.health_request_builder import HealthRequestBuilder
    from .ready.ready_request_builder import ReadyRequestBuilder

class V1RequestBuilder(BaseRequestBuilder):
    """
    Builds and executes requests for operations under /api/v1
    """
    def __init__(self,request_adapter: RequestAdapter, path_parameters: Union[str, dict[str, Any]]) -> None:
        """
        Instantiates a new V1RequestBuilder and sets the default values.
        param path_parameters: The raw url or the url-template parameters for the request.
        param request_adapter: The request adapter to use to execute the requests.
        Returns: None
        """
        super().__init__(request_adapter, "{+baseurl}/api/v1", path_parameters)
    
    @property
    def aps(self) -> ApsRequestBuilder:
        """
        The aps property
        """
        from .aps.aps_request_builder import ApsRequestBuilder

        return ApsRequestBuilder(self.request_adapter, self.path_parameters)
    
    @property
    def health(self) -> HealthRequestBuilder:
        """
        The health property
        """
        from .health.health_request_builder import HealthRequestBuilder

        return HealthRequestBuilder(self.request_adapter, self.path_parameters)
    
    @property
    def ready(self) -> ReadyRequestBuilder:
        """
        The ready property
        """
        from .ready.ready_request_builder import ReadyRequestBuilder

        return ReadyRequestBuilder(self.request_adapter, self.path_parameters)
    

