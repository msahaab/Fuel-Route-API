from rest_framework.exceptions import ParseError
from rest_framework.response import Response
from rest_framework.views import APIView
from drf_spectacular.utils import OpenApiExample, OpenApiParameter, extend_schema

from .services.planner import plan_trip


class RoutePlanView(APIView):
    """Plan an optimal fuel route across the USA."""

    @extend_schema(
        summary="Plan a fuel route (GET)",
        description="Pass start and finish as query parameters. Accepts addresses or lat,lon coordinates.",
        parameters=[
            OpenApiParameter("start", str, description="Origin — address or lat,lon", required=True, examples=[OpenApiExample("OKC coords", value="35.4676,-97.5164")]),
            OpenApiParameter("finish", str, description="Destination — address or lat,lon", required=True, examples=[OpenApiExample("Chicago coords", value="41.8781,-87.6298")]),
        ],
    )
    def get(self, request):
        start = str(request.query_params.get("start") or "").strip()
        finish = str(request.query_params.get("finish") or "").strip()
        return self._plan(start, finish)

    @extend_schema(
        summary="Plan a fuel route (POST)",
        description="Send start and finish in the JSON body. Accepts addresses or lat,lon coordinates.",
        request={
            "application/json": {
                "type": "object",
                "properties": {
                    "start": {"type": "string", "example": "Oklahoma City, OK"},
                    "finish": {"type": "string", "example": "Chicago, IL"},
                },
                "required": ["start", "finish"],
            }
        },
        examples=[
            OpenApiExample(
                "Addresses",
                value={"start": "Oklahoma City, OK", "finish": "Chicago, IL"},
                request_only=True,
            ),
            OpenApiExample(
                "Coordinates",
                value={"start": "35.4676,-97.5164", "finish": "41.8781,-87.6298"},
                request_only=True,
            ),
        ],
    )
    def post(self, request):
        start = str(request.data.get("start") or "").strip()
        finish = str(request.data.get("finish") or "").strip()
        return self._plan(start, finish)

    def _plan(self, start, finish):
        try:
            if not start or not finish:
                raise ValueError("Both 'start' and 'finish' locations are required")
            result = plan_trip(start, finish)
        except (ValueError, ParseError) as exc:
            return Response({"error": str(exc)}, status=400)
        except Exception as exc:  # catch-all for upstream API errors
            return Response({"error": str(exc)}, status=502)
        return Response(result.to_dict())


# keep a reference for url routing
route_plan = RoutePlanView.as_view()
